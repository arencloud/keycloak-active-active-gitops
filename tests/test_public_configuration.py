import json
from pathlib import Path
import shutil
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
import check_configuration


class PublicConfigurationTests(unittest.TestCase):
    def test_public_scaffold_is_valid_but_not_ready_for_deployment(self):
        self.assertEqual(check_configuration.validate(ROOT, True), [])
        errors = check_configuration.validate(ROOT)
        self.assertTrue(any('hub_api' in item for item in errors))
        self.assertTrue(any('administrator_networks' in item for item in errors))
        self.assertTrue(any('buildconfig.yaml' in item for item in errors))

    def test_embedded_secret_is_rejected_without_being_printed(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            shutil.copytree(ROOT / 'automation', root / 'automation')
            path = root / 'automation/aap/profiles/platform.json'
            platform = json.loads(path.read_text())
            platform['repository_url'] = 'https://user:dummy-sensitive-value@git.example.com/repo.git'
            path.write_text(json.dumps(platform))
            errors = check_configuration.validate(root, True)
            self.assertTrue(any('repository_url' in item for item in errors))
            self.assertNotIn('dummy-sensitive-value', str(errors))

    def test_sample_mode_never_approves_enabled_execution(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            shutil.copytree(ROOT / 'automation', root / 'automation')
            path = root / 'automation/aap/profiles/active-active.json'
            profile = json.loads(path.read_text())
            profile['removal_enabled'] = True
            path.write_text(json.dumps(profile))
            self.assertTrue(any('removal_enabled' in item for item in check_configuration.validate(root, True)))

    def test_configured_fork_passes_without_enabling_execution(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for folder in ('automation', 'gitops'):
                shutil.copytree(ROOT / folder, root / folder)
            path = root / 'automation/aap/profiles/platform.json'
            platform = json.loads(path.read_text())
            platform.update(hub_api='https://api.hub.test:6443', opnsense_url='https://router.test:8443',
                            repository_url='https://git.test/team/repo.git',
                            issuer_preference='signer', secret_store_preference='vault-kv',
                            commissioning_database_host='database.test', administrator_networks=['fd00:1234::/64'])
            path.write_text(json.dumps(platform))
            for relative in ('automation/aap/execution-environment/buildconfig.yaml',
                             'automation/aap/controller/container-group-pod.yaml',
                             'gitops/hub/execution/recovery-pvc.yaml'):
                path = root / relative
                import re
                path.write_text(re.sub(r'REPLACE_ME_[A-Z_]+', 'configured-value', path.read_text()))
            self.assertEqual(check_configuration.validate(root), [])
