"""Read-only ACM and MetalLB discovery for the self-service deployment contract."""
import ipaddress
import re
import urllib.parse
from deploy import Failure, require, ready, database_tls

CONDITIONS = ('ManagedClusterJoined', 'ManagedClusterConditionAvailable', 'ManagedClusterConditionClockSynced')


def eligible_clusters(items):
    return sorted(x['metadata']['name'] for x in items
                  if x['metadata']['name'] != 'local-cluster'
                  and x['metadata'].get('labels', {}).get('local-cluster') != 'true'
                  and all(ready(x, c) for c in CONDITIONS))


def api_endpoint(cluster):
    clients = cluster['spec'].get('managedClusterClientConfigs', [])
    require(len(clients) == 1, 'ACM must advertise one managed API endpoint')
    value = clients[0]
    url = urllib.parse.urlsplit(value['url'])
    require(url.scheme == 'https' and url.hostname and not url.username and not url.password
            and not url.query and not url.fragment and value.get('caBundle'), 'Invalid ACM API endpoint/CA')
    return value


def address_range(value):
    if '-' in value:
        first, last = (ipaddress.ip_address(x.strip()) for x in value.split('-', 1))
    else:
        network = ipaddress.ip_network(value)
        first, last = network.network_address, network.broadcast_address
    require(first.version == last.version == 6 and int(first) <= int(last), 'Expected an IPv6 MetalLB range')
    return first, last


def in_pool(address, pool):
    ip = ipaddress.ip_address(address)
    return any(int(a) <= int(ip) <= int(b) for a, b in [address_range(x) for x in pool['spec']['addresses']])


def choose_pool(items, preferred):
    candidates = []
    for pool in items:
        if pool['spec'].get('autoAssign', True) is not True:
            continue
        if not pool['spec'].get('addresses'): continue
        try:
            for value in pool['spec']['addresses']: address_range(value)
        except (Failure, ValueError):
            continue
        candidates.append(pool)
    matches = [p for p in candidates if p['metadata']['name'] == preferred]
    require(len(matches or candidates) == 1, 'No unique automatic IPv6 MetalLB pool; platform configuration needs review')
    return (matches or candidates)[0]


def ready_vault_resource(items, preferred, kind):
    def vault(x):
        return 'vault' in (x.get('spec', {}).get('provider', {}) if kind == 'store' else x.get('spec', {}))
    candidates = [x for x in items if ready(x) and vault(x)]
    preferred_items = [x for x in candidates if x['metadata']['name'] == preferred]
    require(len(preferred_items or candidates) == 1, 'No unique Ready Vault ' + kind)
    return (preferred_items or candidates)[0]


def router_source(interfaces, pool):
    """Select an unambiguous directly connected source; never guess across a default route."""
    ranges = [address_range(x) for x in pool['spec']['addresses']]
    matches = []
    for name, interface in interfaces.items():
        if 'up' not in interface.get('flags', []) or name == 'lo0': continue
        for value in interface.get('ipv6', []):
            if value.get('link-local') or value.get('tentative') or value.get('deprecated'): continue
            ip = ipaddress.IPv6Address(value['ipaddr'])
            network = ipaddress.ip_network(str(ip) + '/' + str(value['subnetbits']), strict=False)
            if all(a in network and b in network for a, b in ranges):
                matches.append((name, str(ip)))
    require(len(matches) == 1, 'MetalLB pool needs one connected OPNsense IPv6 interface/source')
    return matches[0]


def public_interface(interfaces, vip):
    matches = []
    ip = ipaddress.IPv6Address(vip)
    require(ip.is_global, 'Public VIP must be a globally routable IPv6 address')
    for name, interface in interfaces.items():
        if name == 'lo0' or 'up' not in interface.get('flags', []): continue
        for value in interface.get('ipv6', []):
            if value.get('link-local'): continue
            network = ipaddress.ip_network(value['ipaddr'] + '/' + str(value['subnetbits']), strict=False)
            if ip in network: matches.append(name)
    require(len(set(matches)) == 1, 'Public VIP does not identify one connected OPNsense interface')
    return matches[0]


def hub_sources(hub):
    # OVN can preserve pod addresses or SNAT to a node address. Limit access to these
    # discovered hub ranges, not to all routed subnets or the public internet.
    nodes = hub.get('nodes')['items']
    values = {str(ipaddress.ip_address(a['address'])) + '/128' for n in nodes
              for a in n.get('status', {}).get('addresses', [])
              if a['type'] == 'InternalIP' and ipaddress.ip_address(a['address']).version == 6}
    network = hub.get('networks.config.openshift.io', 'cluster')['spec']
    values.update(x['cidr'] for x in network['clusterNetwork'] if ipaddress.ip_network(x['cidr']).version == 6)
    require(values, 'Hub has no discoverable IPv6 execution source ranges')
    return sorted(values)


def connected_route(routes, device, pool):
    networks = []
    for route in routes:
        if route.get('proto') != 'ipv6' or route.get('destination') == 'default': continue
        if route.get('netif') != device or not route.get('gateway', '').startswith('link#'): continue
        try: networks.append(ipaddress.ip_network(route['destination'], strict=False))
        except ValueError: continue
    require(all(any(a in n and b in n for n in networks) for a, b in [address_range(v) for v in pool['spec']['addresses']]), 'Connected IPv6 route to MetalLB pool is missing')


def advertises_pool(advertisement, pool):
    spec = advertisement['spec']
    if not spec.get('ipAddressPools') and not spec.get('ipAddressPoolSelectors'): return True
    if pool['metadata']['name'] in spec.get('ipAddressPools', []): return True
    labels = pool['metadata'].get('labels', {})
    for selector in spec.get('ipAddressPoolSelectors', []):
        match = all(labels.get(k) == v for k, v in selector.get('matchLabels', {}).items())
        for expr in selector.get('matchExpressions', []):
            key, op, values = expr['key'], expr['operator'], expr.get('values', [])
            match = match and {'In': key in labels and labels.get(key) in values, 'NotIn': labels.get(key) not in values, 'Exists': key in labels, 'DoesNotExist': key not in labels}.get(op, False)
        if match: return True
    return False


def public_hostname(value):
    host = value.lower().removesuffix('.')
    labels = host.split('.')
    require(len(host) <= 253 and len(labels) >= 2 and
            all(re.fullmatch(r'[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?', label) for label in labels)
            and not labels[-1].isdigit(),
            'Public FQDN must be a DNS hostname without a scheme, port, path or wildcard')
    return host


def deployment_identity(recorded, desired):
    require(database_tls(recorded) == database_tls(desired), 'Database TLS mode changed; use a planned database transport migration')
    require(recorded.get('namespace') == desired.get('namespace'), 'Deployment namespace changed')
    require(recorded.get('database_path') == desired.get('database_path'), 'Database secret path changed; use a database migration operation')
    require(recorded['environment'] == desired['environment'], 'Deployment state belongs to another environment')
    require(recorded['public_host'] == desired['public_host'], 'Existing deployment uses another FQDN; hostname migration requires a separate operation')
    require(recorded['public_vip'] == desired['public_vip'], 'Existing deployment uses another VIP; remove it before changing the public address')
    require([s['name'] for s in recorded['sites']] == [s['name'] for s in desired['sites']], 'Existing deployment targets another pair; remove it before relocation')
