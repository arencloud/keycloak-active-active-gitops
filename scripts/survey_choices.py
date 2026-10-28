#!/usr/bin/env python3
"""Read ACM inventory for AAP's static choice list; never modifies a cluster."""
import json
import os
from pathlib import Path
from deploy import Kube
from discovery import eligible_clusters

if __name__ == '__main__':
    platform = json.loads((Path(__file__).resolve().parents[1] / 'automation/aap/profiles/platform.json').read_text())
    print(json.dumps(eligible_clusters(Kube(os.environ['KUBECONFIG'], platform['hub_api']).get('managedclusters')['items'])))
