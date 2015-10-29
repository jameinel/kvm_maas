#!/usr/bin/python

from subprocess import check_output, check_call
import xmltodict
import argparse
import os.path
import json
import time
import yaml


def grab(cmd):
    return check_output(cmd, shell=True)


def shell(cmd):
    return check_call(cmd, shell=True)


class KVMMAASNode():
    def __init__(self, settings):
        self.name = settings['machine_name']
        self.path = os.path.join(
            settings['vm_image_path'], self.name + '.qcow2')
        self.settings = settings

    def create_vm(self):
        with open(self.settings['template']) as template:
            conf = xmltodict.parse(template.read())
        conf['domain']['name'] = self.name
        conf['domain']['devices']['disk']['source']['@file'] = self.path
        del(conf['domain']['uuid'])

        with open('node.xml', 'w') as node:
            node.write(xmltodict.unparse(conf))

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
        elif isinstance(interfaces, dict): # actually OrderedDict
            interface = interfaces
        else:
            raise RuntimeError("don't know how to handle interfaces that is a %s".format(
                type(interfaces)))

        self.mac_address = interface['mac']['@address']

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

    def add_to_maas(self):
        print 'Looking for node in MAAS'
        node_not_found = True
        while node_not_found:
            time.sleep(1)
            nodes = json.loads(grab('maas {maas_name} nodes list'.format(**self.settings)))
            for node in nodes:
                # TODO: Cope with >1 network interface
                if node['macaddress_set'][0]['mac_address'] == self.mac_address:
                    self.settings['system_id'] = node['system_id']
                    print 'Setting power control'
                    shell('maas {maas_name} node update {system_id} power_type="virsh" '
                          'power_parameters_power_address=qemu+ssh://{vm_host_user}@{vm_host}/system '
                          'power_parameters_power_id={machine_name} '
                          'hostname={machine_name}'.format(**self.settings))

                    #shell('virsh start ' + args.name)
                    shell('maas {maas_name} nodes accept nodes={system_id}'.format(**self.settings))
                    node_not_found = False

    def new(self):
        self.create_vm()
        self.wait_for_power_off()
        self.add_to_maas()

def configure():
    parser = argparse.ArgumentParser(description='Create a KVM mode for our virtual MAAS cluster.')
    parser.add_argument('name', metavar='N', type=str,
                       help='name of new machine')
    parser.add_argument('--template', '-t', default='template.xml',
                        help='virsh XML template file to use')

    args = parser.parse_args()

    with open(os.path.expanduser('~/.config/kvm_maas.yaml')) as f:
        settings = yaml.load(f)

    settings['machine_name'] = args.name
    settings['template'] = args.template
    return settings

if __name__ == '__main__':
    settings = configure()
    node = KVMMAASNode(settings)
    node.new()
