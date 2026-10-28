"""Scoped edge preparation; no trust-store private keys or API bodies enter logs."""
import base64
import ipaddress
import re
from pathlib import Path
import ssl
import subprocess

from deploy import require, normalize_field, private_file, poll


def inventory(api, path):
    _, response = api.request('GET', path)
    rows = response.get('rows', [])
    require(int(response.get('total', len(rows))) == len(rows), 'Incomplete edge inventory')
    return rows


def interface_id(api, device):
    _, assignments = api.request('GET', '/api/interfaces/assignment/get')
    matches = [name for name, value in assignments['interface']['interface'].items()
               if normalize_field(value['if']) == device]
    require(len(matches) == 1, 'Cannot map physical interface to one OPNsense assignment')
    return matches[0]


def ensure_vip(api, vip, device, journal):
    interface = interface_id(api, device)
    rows = inventory(api, '/api/interfaces/vip_settings/search_item')
    matches = [r for r in rows if (r.get('subnet') or r.get('network', '').split('/')[0] or r.get('address', '').split('/')[0]) == vip]
    require(len(matches) <= 1, 'Ambiguous public VIP')
    if matches:
        _, saved = api.request('GET', '/api/interfaces/vip_settings/get_item/' + matches[0]['uuid'])
        require(normalize_field(saved['vip']['interface']) == interface and normalize_field(saved['vip']['mode']) == 'ipalias', 'Existing VIP has a different interface or mode')
        return
    _, interfaces = api.request('GET', '/api/diagnostics/interface/getInterfaceConfig')
    require(not any(ipaddress.IPv6Address(x['ipaddr']) == ipaddress.IPv6Address(vip) for i in interfaces.values() for x in i.get('ipv6', [])), 'Requested VIP is already an interface address without a matching VIP object')
    journal({'system': 'opnsense', 'operation': 'create-vip-intent', 'vip': vip, 'interface': interface})
    _, saved = api.request('POST', '/api/interfaces/vip_settings/add_item', {'vip': {'interface': interface, 'mode': 'ipalias', 'network': vip + '/128', 'descr': 'Keycloak active-active public VIP', 'nobind': '0', 'noexpand': '0'}})
    require(saved.get('result') == 'saved' and saved.get('uuid'), 'VIP creation failed')
    journal({'system': 'opnsense', 'operation': 'create-vip', 'uuid': saved['uuid'], 'vip': vip})
    _, applied = api.request('POST', '/api/interfaces/vip_settings/reconfigure', {})
    require(applied.get('status') == 'ok', 'VIP interface reconfiguration failed')
    def assigned():
        _, interfaces = api.request('GET', '/api/diagnostics/interface/getInterfaceConfig')
        return any(ipaddress.IPv6Address(x['ipaddr']) == ipaddress.IPv6Address(vip) and not x.get('tentative') and not x.get('duplicated') for x in interfaces.get(device, {}).get('ipv6', []))
    poll(assigned, 60, 'public VIP assignment')


def certificates(pem):
    return re.findall(r'-----BEGIN CERTIFICATE-----.*?-----END CERTIFICATE-----', pem, re.S)


def ensure_certificate(api, host, data, journal, directory, ca, frontend_name='keycloak-public', owner='keycloak-active-active'):
    chain = base64.b64decode(data['tls.crt'], validate=True).decode()
    key = base64.b64decode(data['tls.key'], validate=True).decode()
    cert_file = private_file(directory, 'edge-tls.pem', chain)
    key_file = private_file(directory, 'edge-tls.key', key)
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(cert_file, key_file)  # Require a matching key before any API write.
    valid = subprocess.run(['openssl', 'verify', '-verify_hostname', host, '-CAfile', str(ca), '-untrusted', str(cert_file), str(cert_file)], capture_output=True)
    require(valid.returncode == 0, 'Public certificate is expired, untrusted or lacks the required hostname')
    name = owner + '/' + host
    matches = [r for r in inventory(api, '/api/trust/cert/search') if r.get('descr') == name]
    require(len(matches) <= 1, 'Ambiguous project certificate')
    path = '/api/trust/cert/add'
    if matches:
        uid = matches[0]['uuid']
        _, existing = api.request('GET', '/api/trust/cert/get/' + uid)
        obj = existing['cert']
        if certificates(obj.get('crt_payload', '')) == certificates(chain):
            return obj['refid']
        # Never change a certificate now referenced by another HAProxy frontend.
        frontends = inventory(api, '/api/haproxy/settings/search_frontends')
        for row in frontends:
            if row['name'] == frontend_name: continue
            _, other = api.request('GET', '/api/haproxy/settings/get_frontend/' + row['uuid'])
            require(obj['refid'] not in str(normalize_field(other['frontend'].get('ssl_certificates', ''))), 'Project certificate is shared by another frontend')
        path = '/api/trust/cert/set/' + uid
    journal({'system': 'opnsense', 'operation': 'import-project-certificate-intent', 'hostname': host})
    _, result = api.request('POST', path, {'cert': {'action': 'import', 'descr': name, 'crt_payload': chain, 'prv_payload': key}})
    require(result.get('result') == 'saved', 'Project certificate import failed')
    uid = matches[0]['uuid'] if matches else result.get('uuid')
    require(uid, 'Certificate API did not return an identity')
    _, result = api.request('GET', '/api/trust/cert/get/' + uid)
    require(result['cert'].get('refid'), 'Certificate reference missing')
    journal({'system': 'opnsense', 'operation': 'project-certificate', 'uuid': uid, 'reference': result['cert']['refid']})
    return result['cert']['refid']


def discover_ca(api, ca, directory):
    # Exact certificate match to the issuer's trusted CA bundle, never description matching.
    trusted = {ssl.PEM_cert_to_DER_cert(x) for x in certificates(Path(ca).read_text())}
    matches = []
    for row in inventory(api, '/api/trust/ca/search'):
        _, result = api.request('GET', '/api/trust/ca/get/' + row['uuid'])
        obj = result['ca']
        pem = obj.get('crt_payload') or base64.b64decode(obj['crt'], validate=True).decode()
        if ssl.PEM_cert_to_DER_cert(certificates(pem)[0]) not in trusted: continue
        file = private_file(directory, 'candidate-ca-' + row['uuid'] + '.pem', pem)
        subject = subprocess.check_output(['openssl', 'x509', '-in', str(file), '-noout', '-subject', '-issuer', '-nameopt', 'RFC2253'], text=True).splitlines()
        if subject[0].split('=', 1)[1].strip() == subject[1].split('=', 1)[1].strip(): matches.append(obj['refid'])
    require(len(matches) == 1, 'No unique OPNsense root CA matches the trusted issuer bundle')
    return matches[0]


def ensure_public_rule(api, host, vip, device, journal, owner='keycloak-active-active'):
    """Add one narrow IPv6 HTTPS rule; never rewrite an existing shared rule."""
    fields = {'description': owner + '/' + host, 'enabled': '1',
              'interface': interface_id(api, device), 'action': 'pass', 'direction': 'in',
              'ipprotocol': 'inet6', 'protocol': 'TCP', 'source_net': 'any',
              'destination_net': vip, 'destination_port': '443', 'quick': '1'}
    rows = inventory(api, '/api/firewall/filter/search_rule?rowCount=-1')
    matches = [r for r in rows if r.get('description') == fields['description']]
    require(len(matches) <= 1, 'Ambiguous project firewall rule')
    if matches:
        uid = matches[0]['uuid']
        verify_public_rule(api, {'uuid': uid, 'fields': fields})
    else:
        journal({'system': 'opnsense', 'operation': 'create-https-rule-intent', 'fields': fields})
        _, result = api.request('POST', '/api/firewall/filter/add_rule', {'rule': fields})
        require(result.get('result') == 'saved' and result.get('uuid'), 'Public HTTPS rule creation failed')
        uid = result['uuid']
        journal({'system': 'opnsense', 'operation': 'create-https-rule', 'uuid': uid, 'fields': fields})
    apply_filter(api)
    return {'uuid': uid, 'fields': fields}


def verify_public_rule(api, rule):
    _, current = api.request('GET', '/api/firewall/filter/get_rule/' + rule['uuid'])
    require(all(normalize_field(current['rule'].get(k)) == v for k, v in rule['fields'].items()), 'Project firewall rule changed; refusing to modify it')


def apply_filter(api):
    _, result = api.request('POST', '/api/firewall/filter/apply', {})
    require(str(result.get('status', '')).strip().lower() == 'ok', 'Firewall apply failed')


def remove_public_rule(api, rule, journal):
    verify_public_rule(api, rule)
    journal({'system': 'opnsense', 'operation': 'delete-https-rule-intent', **rule})
    _, result = api.request('POST', '/api/firewall/filter/del_rule/' + rule['uuid'], {})
    require(result.get('result') == 'deleted', 'Project HTTPS rule deletion failed')
    apply_filter(api)


def check_listener_collision(api, vip, frontend_name='keycloak-public'):
    for row in inventory(api, '/api/haproxy/settings/search_frontends'):
        if row.get('name') == frontend_name: continue
        _, result = api.request('GET', '/api/haproxy/settings/get_frontend/' + row['uuid'])
        frontend = result['frontend']
        if normalize_field(frontend.get('enabled')) != '1': continue
        binds = str(normalize_field(frontend.get('bind', ''))).replace(',', ' ').split()
        require(not any(b in ('[' + vip + ']:443', '[::]:443', ':::443', ':443', '*:443') for b in binds), 'Another HAProxy frontend already binds public IPv6 port 443')
