#!/usr/bin/python

from subprocess import check_output, check_call
import collections
import copy
import xmltodict
import argparse
import os.path
import json
import netaddr
import re
import sys
import time
import yaml


debug = False

def grab(cmd):
    if debug:
        print "running: {}".format(cmd)
    return check_output(cmd, shell=True)


def shell(cmd):
    if debug:
        print "running: {}".format(cmd)
    return check_call(cmd, shell=True)


class VirshNetwork():
    """Represent information we care about for a virsh network object."""

    @classmethod
    def from_name(cls, name):
        """Instantiate a VirshNetwork based on a name, and determine the rest of the info."""
        info = xmltodict.parse(grab('virsh net-dumpxml ' + name))
        ip_info = info['network']['ip']
        cidr = netaddr.IPNetwork(ip_info['@address'] + '/' + ip_info['@netmask'])
        return cls(name, cidr)

    @classmethod
    def all_networks(cls):
        """Find all the networks that virsh knows about."""
        # Unfortunately I haven't really found a programatic way to get this list
        net_list = grab('virsh net-list').splitlines()
        assert net_list[0].startswith(' Name'), repr(net_list[0])
        assert net_list[1].startswith('------'), repr(net_list[1])
        names = []
        name_re = re.compile('^\s*(?P<name>\w+)\s.*$')
        for line in net_list[2:]:
            if not line:
                continue
            m = name_re.match(line)
            if m is None:
                print 'Unknown line from "virsh net-list": {!r}'.format(line)
                continue
            name = m.group('name')
            names.append(name)
        known_networks = [cls.from_name(name) for name in names]
        return dict((vn.cidr, vn) for vn in known_networks)

    def __init__(self, name, cidr):
        self.name = name
        self.cidr = cidr

    def __repr__(self):
        return '{}({}, {})'.format(self.__class__.__name__, self.name, self.cidr)


class MAASSubnet():
    """Information about a MAAS Subnet object."""

    @classmethod
    def all_subnets(cls, settings):
        """Create a list of MAASSubnets from the given MAAS server."""
        subnet_info = json.loads(grab('maas {maas_name} subnets read'.format(**settings)))
        known_subnets = [cls(si['name'], si['space'], netaddr.IPNetwork(si['cidr']), si['id'])
                         for si in subnet_info]
        return dict((sn.cidr, sn) for sn in known_subnets)

    def __init__(self, name, space, cidr, maas_id):
        self.name = name
        self.space = space
        self.cidr = cidr
        self.maas_id = maas_id

    def __repr__(self):
        return '{}({}, {}, {}, {})'.format(self.__class__.__name__,
                self.name, self.space, self.cidr, self.maas_id)


class KVMMAASNode():
    def __init__(self, settings, virsh_networks, maas_subnets):
        self.name = settings['machine_name']
        self.path = os.path.join(
            settings['vm_image_path'], self.name + '.qcow2')
        self.settings = settings
        self.mac_address = None
        self.mac_addresses = []
        self.virsh_networks = virsh_networks
        self.maas_subnets = maas_subnets

    def _setup_vm_interfaces(self, conf):
        # Don't touch the template if the user didn't supply anything
        subnets = self.settings['subnets']
        if len(subnets) == 0:
            return
        template_interface = conf['domain']['devices']['interface']
        if isinstance(template_interface, list):
            template_interface = template_interface[0]
        # TODO: We don't check for slot collisions
        slot = int(template_interface['address']['@slot'], base=16)
        interfaces = []
        for cidr in subnets:
            ip_net = netaddr.IPNetwork(cidr)
            interface_def = copy.deepcopy(template_interface)
            virsh_net = self.virsh_networks[ip_net]
            interface_def['source']['@network'] = virsh_net.name
            interface_def['address']['@slot'] = '0x{:02x}'.format(slot)
            slot += 1
            interfaces.append(interface_def)
        conf['domain']['devices']['interface'] = interfaces

    def create_vm(self):
        with open(self.settings['template']) as template:
            conf = xmltodict.parse(template.read())
        conf['domain']['name'] = self.name
        conf['domain']['devices']['disk']['source']['@file'] = self.path
        del(conf['domain']['uuid'])
        self._setup_vm_interfaces(conf)

        with open('node.xml', 'w') as node:
            node.write(xmltodict.unparse(conf, pretty=True))

        shell('qemu-img create -f qcow2 ' + self.path + ' 32G')
        shell('virsh define node.xml')
        print 'node defined, starting'
        shell('virsh start ' + self.name)
        print 'node started'

        # We now have a new node. Find its MAC address so we can identify it in MAAS
        conf = xmltodict.parse(grab('virsh dumpxml ' + self.name))

        interfaces = conf['domain']['devices']['interface']
        if isinstance(interfaces, list):
            # Just grab the mac_address of the first interface, we'll just
            # require it to be the one that boots and can find MAAS for now
            interface = interfaces[0]
            for interface in interfaces:
                self.mac_addresses.append(interface['mac']['@address'])
        elif isinstance(interfaces, dict): # actually OrderedDict
            interface = interfaces
            self.mac_addresses = [interface['mac']['@address']]
        else:
            raise RuntimeError("don't know how to handle interfaces that is a %s".format(
                type(interfaces)))

        self.mac_address = self.mac_addresses[0]

    def wait_for_power_off(self):
        print 'Waiting for node to finish initial boot'
        off_count = 0
        while True:
            state = grab('virsh domstate ' + self.name).rstrip()
            if state == 'shut off':
                off_count += 1
                if off_count == 3:
                    break
            elif state != 'running' and state != 'in shutdown':
                print 'Unexpected machine state from "virsh domstate %s"' % self.name
                print state
                print 'Aborting...'
                exit(1)
            else:
                off_count = 0
            time.sleep(1)


    def _update_maas_record(self, node):
        self.settings['system_id'] = node['system_id']
        print 'Setting power control'
        shell('maas {maas_name} node update {system_id} power_type="virsh" '
              'power_parameters_power_address=qemu+ssh://{vm_host_user}@{vm_host}/system '
              'power_parameters_power_id={machine_name} '
              'hostname={machine_name}'.format(**self.settings))

        shell('maas {maas_name} nodes accept nodes={system_id}'.format(**self.settings))


    def add_to_maas(self):
        print 'Looking for node in MAAS'
        node_not_found = True
        while node_not_found:
            time.sleep(1)
            nodes = json.loads(grab('maas {maas_name} nodes list'.format(**self.settings)))
            for node in nodes:
                for mac_info in node['macaddress_set']:
                    if mac_info['mac_address'] == self.mac_address:
                        self._update_maas_record(node)
                        node_not_found = False

    def _wait_for_status(self, desired_status):
        while True:
            time.sleep(1)
            node = json.loads(grab('maas {maas_name} node read {system_id}'.format(**self.settings)))
            status = node['substatus_name']
            if status not in ('New', 'Ready', 'Commissioning'): # Not ready yet
                print 'unknown node substatus: {!r}'.format(status)
                exit(1)
            if debug:
                print 'status: {!r}'.format(status)
            if status == desired_status:
                return

    def update_maas_networking(self):
        """Update the subnets for the various interfaces. Must be called after
        commissioning finishes.
        """
        print 'Waiting for node to be ready'
        self._wait_for_status('Ready')
        interfaces = json.loads(grab('maas {maas_name} node-interfaces read {system_id}'.format(**self.settings)))
        subnets = self.settings['subnets']
        for interface in interfaces:
            mac_address = interface['mac_address']
            subnet_cidr = subnets[self.mac_addresses.index(mac_address)]
            net = netaddr.IPNetwork(subnet_cidr)
            maas_subnet = self.maas_subnets[net]
            found = False
            for link in interface['links']:
                if 'subnet' not in link:
                    continue
                cidr = link['subnet']['cidr']
                if cidr == subnet_cidr:
                    found = True
                    break
            if not found:
                params = self.settings.copy()
                params['interface_id'] = interface['id']
                params['subnet_id'] = maas_subnet.maas_id
                shell('maas {maas_name} node-interface link-subnet '
                      '{system_id} {interface_id} '
                      'mode=AUTO subnet={subnet_id}'.format(**params))


    def new(self):
        self.create_vm()
        self.wait_for_power_off()
        self.add_to_maas()
        self.update_maas_networking()


def configure():
    parser = argparse.ArgumentParser(description='Create a KVM mode for our virtual MAAS cluster.')
    parser.add_argument('name', metavar='N', type=str,
                       help='name of new machine')
    parser.add_argument('--template', '-t', metavar='T', default='template.xml',
                        help='virsh XML template file to use')
    parser.add_argument('--subnet', '-s', metavar='N', action='append', default=[],
                        help='The CIDR each interface should be on. This will be'
                             ' mapped to the VIRSH network and the MAAS subnet.'
                             ' This can be supplied multiple times.')
    parser.add_argument('--debug', action='store_true',
                        help='If true, show commands that are executed.')

    args = parser.parse_args()
    global debug
    debug = args.debug

    with open(os.path.expanduser('~/.config/kvm_maas.yaml')) as f:
        settings = yaml.load(f)

    settings['machine_name'] = args.name
    settings['template'] = args.template
    settings['subnets'] = args.subnet
    return settings


def check_known_cidrs(subnets, virsh_networks, maas_subnets):
    unknown_in_virsh = []
    unknown_in_maas = []
    for cidr in subnets:
        net = netaddr.IPNetwork(cidr)
        if net not in virsh_networks:
            unknown_in_virsh.append(cidr)
        if net not in maas_subnets:
            unknown_in_maas.append(cidr)
    if unknown_in_virsh:
        print 'virsh does not have a network for: {}'.format(unknown_in_virsh)
        print 'it has: {}'.format([str(s) for s in sorted(virsh_networks.keys())])
    if unknown_in_maas:
        print 'maas does not have a subnet for: {}'.format(unknown_in_maas)
        print 'it has: {}'.format([str(s) for s in sorted(maas_subnets.keys())])
    if unknown_in_virsh or unknown_in_maas:
        sys.exit(1)

if __name__ == '__main__':
    settings = configure()
    virsh_networks = VirshNetwork.all_networks()
    maas_subnets = MAASSubnet.all_subnets(settings)
    check_known_cidrs(settings['subnets'], virsh_networks, maas_subnets)
    # TODO: we should probably have a way to describe virsh networks to maas (or vice
    # versa).
    node = KVMMAASNode(settings, virsh_networks, maas_subnets)
    node.new()
