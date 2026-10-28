#!/usr/bin/env python3
"""Render review artifacts only. Never talks to a cluster or applies manifests."""
import argparse
import json
from pathlib import Path
import re
import subprocess

ROOT = Path(__file__).resolve().parents[1]
TARGETS = {
    'hub-registration': 'gitops/hub/registration',
    'hub-project': 'gitops/hub/project',
    'applications': 'gitops/applications',
    'operator': 'gitops/operator',
    'prerequisites': 'gitops/keycloak/prerequisites',
    'cluster-a': 'gitops/keycloak/overlays/cluster-a',
    'cluster-b': 'gitops/keycloak/overlays/cluster-b',
}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--revision', required=True, help='Full reviewed Git commit SHA')
    parser.add_argument('--output', required=True, type=Path)
    args = parser.parse_args()
    if not re.fullmatch(r'[0-9a-f]{40}', args.revision):
        parser.error('--revision must be a full 40-character commit SHA')
    # Do not label arbitrary local files as a reviewed release.
    head = subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=ROOT, text=True).strip()
    if head != args.revision:
        parser.error('Check out the requested revision before rendering')
    changes = subprocess.check_output(
        ['git', 'status', '--porcelain', '--untracked-files=normal'], cwd=ROOT, text=True)
    if changes.strip():
        parser.error('Release rendering requires a clean checkout, including tracked scaffolding')
    output = args.output.resolve()
    if output == ROOT or ROOT in output.parents:
        parser.error('Use an artifact directory outside the repository')
    rendered = {}
    unresolved = {}
    for name, path in TARGETS.items():
        result = subprocess.run(['kubectl', 'kustomize', str(ROOT / path)],
                                text=True, capture_output=True, check=True)
        text = result.stdout.replace('REPLACE_ME_REVIEWED_REVISION', args.revision)
        rendered[name] = text
        unresolved[name] = sorted(set(re.findall(r'REPLACE_ME_[A-Z0-9_]+', text)))
    output.mkdir(parents=True, exist_ok=True)
    for name, text in rendered.items():
        (output / (name + '.yaml')).write_text(text)
    report = {
        'revision': args.revision,
        'applied': False,
        'inputs_resolved': not any(unresolved.values()),
        'live_validation_performed': False,
        'unresolved_inputs': {k: v for k, v in unresolved.items() if v},
    }
    (output / 'report.json').write_text(json.dumps(report, indent=2) + '\n')
    print(json.dumps(report, indent=2))
    # Draft artifacts are still useful, but must not advance a release workflow.
    return 2 if any(unresolved.values()) else 0


if __name__ == '__main__':
    raise SystemExit(main())
