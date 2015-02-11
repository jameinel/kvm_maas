# MAAS in KVM node creator
Helper to create KVM nodes for a virtual MAAS deployment. Creates a VM, network boots it (MAAS takes care of this),
adds it as a node and commissions it. Sets up power on/off from MAAS to KVM so it can power up your VMs and check their
power state. It doesn't set up MAAS for you. See http://www.teale.de/tealeg/computing/cloud/kvm_maas_juju_openstack.html
for some good instructions; this script takes care of section 5, 'Create a VM as a client'. 

To configure create ~/.config/kvm_maas.yaml with the following content:
```yaml
maas_name: <name of your maas, which the maas command line is logged into>
vm_host: <host name or IP address of the KVM host>
vm_host_user: <user name for SSHing into vm_host>
vm_image_path: <where to store the KVM disk images>
```
Assuming you have done all steps up to section 5 in http://www.teale.de/tealeg/computing/cloud/kvm_maas_juju_openstack.html
then you can add a node like this:
```bash
./kmaas.py maas_node_1
```
Wait and watch the MAAS web UI. Things will happen.
