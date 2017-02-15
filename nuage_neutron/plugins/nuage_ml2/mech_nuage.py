# Copyright 2015 Alcatel-Lucent USA Inc.
#
#    Licensed under the Apache License, Version 2.0 (the "License"); you may
#    not use this file except in compliance with the License. You may obtain
#    a copy of the License at
#
#         http://www.apache.org/licenses/LICENSE-2.0
#
#    Unless required by applicable law or agreed to in writing, software
#    distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
#    WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
#    License for the specific language governing permissions and limitations
#    under the License.

import inspect
import netaddr

from oslo_config import cfg
from oslo_db.exception import DBDuplicateEntry
from oslo_log import helpers as log_helpers
from oslo_log import log
from oslo_utils import excutils

from neutron._i18n import _
from neutron.api import extensions as neutron_extensions
from neutron.callbacks import resources
from neutron.db import db_base_plugin_v2
from neutron.extensions import portbindings
from neutron.extensions import portsecurity
from neutron.ipam.drivers.neutrondb_ipam import driver
from neutron.ipam import requests as ipam_req
from neutron.manager import NeutronManager
from neutron.plugins.common import constants as p_constants
from neutron.plugins.ml2 import driver_api as api
from neutron_lib.api import validators as lib_validators
from neutron_lib import constants as os_constants


from nuage_neutron.plugins.common.addresspair import NuageAddressPair
from nuage_neutron.plugins.common import base_plugin
from nuage_neutron.plugins.common import constants
from nuage_neutron.plugins.common.exceptions import NuageBadRequest
from nuage_neutron.plugins.common import extensions
from nuage_neutron.plugins.common import nuagedb
from nuage_neutron.plugins.common import utils
from nuage_neutron.plugins.common.utils import handle_nuage_api_errorcode
from nuage_neutron.plugins.common.utils import ignore_no_update
from nuage_neutron.plugins.common.utils import ignore_not_found
from nuage_neutron.plugins.common.validation import Is
from nuage_neutron.plugins.common.validation import IsSet
from nuage_neutron.plugins.common.validation import require
from nuage_neutron.plugins.common.validation import validate
from nuage_neutron.plugins.nuage_ml2 import extensions  # noqa
from nuage_neutron.plugins.nuage_ml2.securitygroup import NuageSecurityGroup

LB_DEVICE_OWNER_V2 = os_constants.DEVICE_OWNER_LOADBALANCERV2

LOG = log.getLogger(__name__)


class NuageMechanismDriver(base_plugin.RootNuagePlugin,
                           api.MechanismDriver,
                           db_base_plugin_v2.NeutronDbPluginV2):

    def initialize(self):
        LOG.debug('Initializing driver')
        neutron_extensions.append_api_extensions_path(extensions.__path__)
        self.init_vsd_client()
        self._wrap_nuageclient()
        self._core_plugin = None
        self._default_np_id = None
        NuageSecurityGroup().register()
        NuageAddressPair().register()
        db_base_plugin_v2.AUTO_DELETE_PORT_OWNERS += [
            constants.DEVICE_OWNER_DHCP_NUAGE]
        LOG.debug('Initializing complete')

    @property
    def core_plugin(self):
        if self._core_plugin is None:
            self._core_plugin = NeutronManager.get_plugin()
        return self._core_plugin

    @property
    def default_np_id(self):
        if self._default_np_id is None:
            self._default_np_id = NeutronManager.get_service_plugins()[
                constants.NUAGE_APIS].default_np_id
        return self._default_np_id

    def _wrap_nuageclient(self):
        """Wraps nuagecient methods with try-except to ignore certain errors.

        When updating an entity on the VSD and there is nothing to actually
        update because the values don't change, VSD will throw an error. This
        is not needed for neutron so all these exceptions are ignored.

        When VSD responds with a 404, this is sometimes good (for example when
        trying to update an entity). Yet sometimes this is not required to be
        an actual exception. When deleting an entity that does no longer exist
        it is fine for neutron. Also when trying to retrieve something from VSD
        having None returned is easier to work with than RESTProxy exceptions.
        """

        methods = inspect.getmembers(self.nuageclient,
                                     lambda x: inspect.ismethod(x))
        for m in methods:
            wrapped = ignore_no_update(m[1])
            if m[0].startswith('get_') or m[0].startswith('delete_'):
                wrapped = ignore_not_found(wrapped)
            setattr(self.nuageclient, m[0], wrapped)

    @handle_nuage_api_errorcode
    def create_subnet_postcommit(self, context):
        subnet = context.current
        network = context.network.current
        db_context = context._plugin_context

        self._validate_create_subnet(db_context, network, subnet)
        if subnet.get('nuagenet') and subnet.get('net_partition'):
            self._create_vsd_managed_subnet(db_context, subnet)
        else:
            self._create_openstack_managed_subnet(db_context, subnet)

        if subnet['underlay'] == os_constants.ATTR_NOT_SPECIFIED:
            subnet['underlay'] = None

    def _create_vsd_managed_subnet(self, context, subnet):
        nuage_subnet_id = subnet['nuagenet']
        original_gateway = subnet['gateway_ip']
        nuage_npid = self._validate_net_partition(subnet, context)
        nuage_subnet, shared_subnet = self._get_nuage_subnet(nuage_subnet_id)
        self._validate_cidr(subnet, nuage_subnet, shared_subnet)
        self._set_gateway_from_vsd(nuage_subnet, shared_subnet, subnet)
        result = self.nuageclient.attach_nuage_group_to_nuagenet(
            context.tenant, nuage_npid, nuage_subnet_id,
            subnet.get('shared'))
        (nuage_uid, nuage_gid) = result
        try:
            with context.session.begin(subtransactions=True):
                self._update_gw_and_pools(self.core_plugin, context, subnet,
                                          original_gateway)
                self._reserve_dhcp_ip(self.core_plugin, context, subnet,
                                      nuage_subnet, shared_subnet)
                nuagedb.add_subnetl2dom_mapping(
                    context.session, subnet['id'], nuage_subnet_id,
                    nuage_npid, nuage_user_id=nuage_uid,
                    nuage_group_id=nuage_gid, managed=True)
                subnet['vsd_managed'] = True
        except DBDuplicateEntry:
            self._cleanup_group(context, nuage_npid, nuage_subnet_id,
                                subnet)
            msg = _("Multiple OpenStack Subnets cannot be linked to the same "
                    "Nuage Subnet")
            raise NuageBadRequest(msg=msg)
        except Exception:
            self._cleanup_group(context, nuage_npid, nuage_subnet_id,
                                subnet)
            raise

    def _create_openstack_managed_subnet(self, context, subnet):
        core_plugin = NeutronManager.get_plugin()
        network_external = core_plugin._network_is_external(
            context,
            subnet['network_id'])

        if network_external:
            return self._create_nuage_sharedresource(
                context, subnet, constants.SR_TYPE_FLOATING)

        net_partition = self._get_net_partition_for_subnet(context, subnet)
        self._create_nuage_subnet(context, subnet, net_partition['id'], None)

    def _get_net_partition_for_subnet(self, context, subnet):
        ent = subnet.get('net_partition', None)
        if not ent:
            net_partition = nuagedb.get_net_partition_by_id(context.session,
                                                            self.default_np_id)
        else:
            net_partition = (
                nuagedb.get_net_partition_by_id(context.session,
                                                subnet['net_partition'])
                or
                nuagedb.get_net_partition_by_name(context.session,
                                                  subnet['net_partition'])
            )
        if not net_partition:
            msg = _('Either net_partition is not provided with subnet OR '
                    'default net_partition is not created at the start')
            raise NuageBadRequest(resource='subnet', msg=msg)
        return net_partition

    @log_helpers.log_method_call
    def _create_nuage_subnet(self, context, neutron_subnet, netpart_id,
                             pnet_binding):
        gw_port = None
        neutron_net = self.core_plugin.get_network(
            context,
            neutron_subnet['network_id'])
        net = netaddr.IPNetwork(neutron_subnet['cidr'])

        params = {
            'netpart_id': netpart_id,
            'tenant_id': neutron_subnet['tenant_id'],
            'net': net,
            'pnet_binding': pnet_binding,
            'shared': neutron_net['shared']
        }

        if neutron_subnet.get('enable_dhcp'):
            last_address = neutron_subnet['allocation_pools'][-1]['end']
            gw_port = self._reserve_ip(self.core_plugin,
                                       context,
                                       neutron_subnet,
                                       last_address)
            params['dhcp_ip'] = gw_port['fixed_ips'][0]['ip_address']
        else:
            LOG.warning(_("CIDR parameter ignored for unmanaged subnet "))
            LOG.warning(_("Allocation Pool parameter ignored"
                          " for unmanaged subnet "))
            params['dhcp_ip'] = None

        try:
            nuage_subnet = self.nuageclient.create_subnet(neutron_subnet,
                                                          params)
        except Exception:
            with excutils.save_and_reraise_exception():
                if gw_port:
                    LOG.debug(_("Deleting gw_port %s") % gw_port['id'])
                    self.core_plugin.delete_port(context, gw_port['id'])

        if nuage_subnet:
            l2dom_id = str(nuage_subnet['nuage_l2template_id'])
            user_id = nuage_subnet['nuage_userid']
            group_id = nuage_subnet['nuage_groupid']
            nuage_id = nuage_subnet['nuage_l2domain_id']
            with context.session.begin(subtransactions=True):
                nuagedb.add_subnetl2dom_mapping(context.session,
                                                neutron_subnet['id'],
                                                nuage_id,
                                                netpart_id,
                                                l2dom_id=l2dom_id,
                                                nuage_user_id=user_id,
                                                nuage_group_id=group_id)
            neutron_subnet['net_partition'] = netpart_id
            neutron_subnet['nuagenet'] = nuage_id

    def _create_nuage_sharedresource(self, context, subnet, type):
        net_id = subnet['network_id']
        self._validate_nuage_sharedresource(context, net_id)

        net = netaddr.IPNetwork(subnet['cidr'])
        params = {
            'neutron_subnet': subnet,
            'net': net,
            'type': type,
            'net_id': net_id,
            'underlay_config': cfg.CONF.RESTPROXY.nuage_fip_underlay
        }
        if subnet.get('underlay') in [True, False]:
            params['underlay'] = subnet.get('underlay')
            subnet['underlay'] = subnet.get('underlay')
        else:
            subnet['underlay'] = params['underlay_config']

        if subnet.get('nuage_uplink'):
            params['nuage_uplink'] = subnet.get('nuage_uplink')
            subnet['nuage_uplink'] = subnet.get('nuage_uplink')
        elif cfg.CONF.RESTPROXY.nuage_uplink:
            subnet['nuage_uplink'] = cfg.CONF.RESTPROXY.nuage_uplink
            params['nuage_uplink'] = cfg.CONF.RESTPROXY.nuage_uplink

        self.nuageclient.create_nuage_sharedresource(params)

    @utils.context_log
    def update_subnet_precommit(self, context):
        updated_subnet = context.current
        original_subnet = context.original
        db_context = context._plugin_context
        subnet_mapping = nuagedb.get_subnet_l2dom_by_id(db_context.session,
                                                        updated_subnet['id'])
        if subnet_mapping and subnet_mapping['nuage_managed_subnet']:
            raise NuageBadRequest(
                msg=_("Subnet %s is a VSD-managed subnet. Update is not "
                      "supported") % updated_subnet['id'])

        net_id = original_subnet['network_id']
        network_external = self.core_plugin._network_is_external(db_context,
                                                                 net_id)

        if network_external:
            return self._update_ext_network_subnet(updated_subnet['id'],
                                                   net_id,
                                                   updated_subnet)
        if subnet_mapping['nuage_managed_subnet']:
            msg = ("Subnet %s is a VSD-Managed subnet."
                   " Update is not supported." % subnet_mapping['subnet_id'])
            raise NuageBadRequest(resource='subnet', msg=msg)
        if not network_external and updated_subnet.get('underlay') is not None:
            msg = _("underlay attribute can not be set for internal subnets")
            raise NuageBadRequest(msg=msg)

        params = {
            'parent_id': subnet_mapping['nuage_subnet_id'],
            'type': subnet_mapping['nuage_l2dom_tmplt_id']
        }

        curr_enable_dhcp = original_subnet.get('enable_dhcp')
        updated_enable_dhcp = updated_subnet.get('enable_dhcp')

        if not curr_enable_dhcp and updated_enable_dhcp:
            last_address = updated_subnet['allocation_pools'][-1]['end']
            gw_port = self._reserve_ip(self.core_plugin,
                                       db_context,
                                       updated_subnet,
                                       last_address)
            params['net'] = netaddr.IPNetwork(original_subnet['cidr'])
            params['dhcp_ip'] = gw_port['fixed_ips'][0]['ip_address']
        elif curr_enable_dhcp and not updated_enable_dhcp:
            params['dhcp_ip'] = None
            filters = {
                'fixed_ips': {'subnet_id': [updated_subnet['id']]},
                'device_owner': [constants.DEVICE_OWNER_DHCP_NUAGE]
            }
            gw_ports = self.core_plugin.get_ports(db_context, filters=filters)
            self._delete_port_gateway(db_context, gw_ports)
        self.nuageclient.update_subnet(updated_subnet, params)

    def _update_ext_network_subnet(self, id, net_id, subnet):
        nuage_params = {
            'subnet_name': subnet.get('name'),
            'net_id': net_id,
            'gateway_ip': subnet.get('gateway_ip')
        }
        self.nuageclient.update_nuage_sharedresource(id, nuage_params)
        nuage_subnet = self.nuageclient.get_sharedresource(id)
        subnet['underlay'] = nuage_subnet['underlay']

    @log_helpers.log_method_call
    def _delete_port_gateway(self, context, ports):
        for port in ports:
            db_base_plugin_v2.NeutronDbPluginV2.delete_port(self.core_plugin,
                                                            context,
                                                            port['id'])

    @utils.context_log
    def delete_subnet_precommit(self, context):
        """Get subnet_l2dom_mapping for later.

        In postcommit this nuage_subnet_l2dom_mapping is no longer available
        because it is set to CASCADE with the subnet. So this row will be
        deleted prior to delete_subnet_postcommit
        """
        subnet = context.current
        db_context = context._plugin_context
        context.nuage_mapping = nuagedb.get_subnet_l2dom_by_id(
            db_context.session, subnet['id'])
        filters = {
            'fixed_ips': {'subnet_id': [subnet['id']]},
            'device_owner': constants.DEVICE_OWNER_DHCP_NUAGE
        }
        context.nuage_ports = self.get_ports(db_context, filters)

    @handle_nuage_api_errorcode
    def delete_subnet_postcommit(self, context):
        db_context = context._plugin_context
        subnet = context.current
        mapping = context.nuage_mapping
        network_external = self.core_plugin._network_is_external(
            db_context,
            subnet['network_id'])

        if network_external:
            self.nuageclient.delete_nuage_sharedresource(subnet['id'])
        elif mapping:
            if not mapping['nuage_managed_subnet']:
                self.nuageclient.delete_subnet(subnet['id'])
            self._cleanup_group(db_context, mapping['net_partition_id'],
                                mapping['nuage_subnet_id'], subnet)

        self._delete_port_gateway(context, context.nuage_ports)

    @handle_nuage_api_errorcode
    @utils.context_log
    def create_port_postcommit(self, context):
        db_context = context._plugin_context
        core_plugin = context._plugin
        port = context.current
        if 'request_port' not in port:
            return
        is_network_external = context.network._network.get('router:external')
        if is_network_external and (port.get('device_owner') not in
                                    constants.AUTO_CREATE_PORT_OWNERS):
            msg = "Cannot create port in a FIP pool Subnet"
            raise NuageBadRequest(resource='port', msg=msg)
        request_port = port['request_port']
        del port['request_port']

        valid = self._validate_port(db_context, port, constants.BEFORE_CREATE)
        subnet_mapping = self.get_subnet_mapping_by_port(db_context, port)

        if (not (subnet_mapping and valid) or
                port.get('device_owner') == constants.DEVICE_OWNER_IRONIC):
            return

        nuage_vport = nuage_vm = np_name = None
        try:
            np_id = subnet_mapping['net_partition_id']
            nuage_subnet, _ = self._get_nuage_subnet(
                subnet_mapping['nuage_subnet_id'])
            if self._port_should_have_vm(port):
                self._validate_vmports_same_netpartition(core_plugin,
                                                         db_context,
                                                         port, np_id)
                desc = ("device_owner:" + constants.NOVA_PORT_OWNER_PREF +
                        "(please do not edit)")
                nuage_vport = self._create_nuage_vport(port, nuage_subnet,
                                                       desc)
                np_name = self.nuageclient.get_net_partition_name_by_id(np_id)
                require(np_name, "netpartition", np_id)
                nuage_vm = self._create_nuage_vm(
                    core_plugin, db_context, port, np_name, subnet_mapping,
                    nuage_vport, nuage_subnet)
            else:
                nuage_vport = self._create_nuage_vport(port, nuage_subnet)
        except Exception:
            if nuage_vm:
                self._delete_nuage_vm(core_plugin, db_context, port, np_name,
                                      subnet_mapping)
            if nuage_vport:
                self.nuageclient.delete_nuage_vport(nuage_vport.get('ID'))
            raise
        rollbacks = []
        try:
            self.nuage_callbacks.notify(resources.PORT, constants.AFTER_CREATE,
                                        self, context=db_context, port=port,
                                        vport=nuage_vport, rollbacks=rollbacks,
                                        request_port=request_port,
                                        subnet_mapping=subnet_mapping)
            if (request_port.get('nuage_redirect-targets') !=
                    os_constants.ATTR_NOT_SPECIFIED):
                self.core_plugin.update_port_status(
                    db_context,
                    port['id'],
                    os_constants.PORT_STATUS_ACTIVE)
        except Exception:
            with excutils.save_and_reraise_exception():
                    for rollback in reversed(rollbacks):
                        rollback[0](*rollback[1], **rollback[2])

    @handle_nuage_api_errorcode
    @utils.context_log
    def update_port_precommit(self, context):
        db_context = context._plugin_context
        core_plugin = context._plugin
        port = context.current
        original = context.original
        if 'request_port' not in port:
            return
        request_port = port['request_port']
        del port['request_port']

        valid = self._validate_port(db_context, port, constants.BEFORE_UPDATE)
        subnet_mapping = self.get_subnet_mapping_by_port(db_context, port)
        if (not (subnet_mapping and valid) or
                port.get('device_owner') == constants.DEVICE_OWNER_IRONIC):
            return
        nuage_vport = self._get_nuage_vport(port, subnet_mapping)

        device_added = device_removed = False
        if not original['device_owner'] and port['device_owner']:
            device_added = True
        elif original['device_owner'] and not port['device_owner']:
            device_removed = True

        if device_added or device_removed:
            np_name = self.nuageclient.get_net_partition_name_by_id(
                subnet_mapping['net_partition_id'])
            require(np_name, "netpartition",
                    subnet_mapping['net_partition_id'])

            if device_removed:
                if self._port_should_have_vm(original):
                    self._delete_nuage_vm(core_plugin, db_context, original,
                                          np_name, subnet_mapping,
                                          is_port_device_owner_removed=True)
            elif device_added:
                if port['device_owner'].startswith(
                        constants.NOVA_PORT_OWNER_PREF):
                    nuage_subnet, _ = self._get_nuage_subnet(
                        subnet_mapping['nuage_subnet_id'])
                    self._create_nuage_vm(core_plugin, db_context, port,
                                          np_name, subnet_mapping, nuage_vport,
                                          nuage_subnet)
        if not subnet_mapping['nuage_managed_subnet']:
            self._process_port_create_secgrp_for_port_sec(db_context, port)
        rollbacks = []
        try:
            self.nuage_callbacks.notify(resources.PORT, constants.AFTER_UPDATE,
                                        core_plugin, context=db_context,
                                        updated_port=port,
                                        original_port=original,
                                        request_port=request_port,
                                        vport=nuage_vport, rollbacks=rollbacks,
                                        subnet_mapping=subnet_mapping)
        except Exception:
            with excutils.save_and_reraise_exception():
                for rollback in reversed(rollbacks):
                    rollback[0](*rollback[1], **rollback[2])

    @utils.context_log
    def delete_port_postcommit(self, context):
        db_context = context._plugin_context
        core_plugin = context._plugin
        port = context.current

        subnet_mapping = self.get_subnet_mapping_by_port(db_context, port)
        if not subnet_mapping:
            return

        if self._port_should_have_vm(port):
            np_name = self.nuageclient.get_net_partition_name_by_id(
                subnet_mapping['net_partition_id'])
            require(np_name, "netpartition",
                    subnet_mapping['net_partition_id'])
            self._delete_nuage_vm(core_plugin, db_context, port, np_name,
                                  subnet_mapping,
                                  is_port_device_owner_removed=True)
        nuage_vport = self._get_nuage_vport(port, subnet_mapping,
                                            required=False)
        if nuage_vport and nuage_vport.get('type') == constants.VM_VPORT:
            try:
                self.nuageclient.delete_nuage_vport(
                    nuage_vport['ID'])
            except Exception as e:
                LOG.error("Failed to delete vport from vsd {vport id: %s}"
                          % nuage_vport['ID'])
                raise e
            rollbacks = []
            try:
                self.nuage_callbacks.notify(
                    resources.PORT, constants.AFTER_DELETE,
                    core_plugin, context=db_context,
                    updated_port=port,
                    port=port,
                    subnet_mapping=subnet_mapping)
            except Exception:
                with excutils.save_and_reraise_exception():
                    for rollback in reversed(rollbacks):
                        rollback[0](*rollback[1], **rollback[2])
        else:
            self.delete_gw_host_vport(db_context, port, subnet_mapping)
            return

    @utils.context_log
    def bind_port(self, context):
        vnic_type = context.current.get(portbindings.VNIC_TYPE,
                                        portbindings.VNIC_NORMAL)
        if vnic_type not in self._supported_vnic_types():
            LOG.debug("Cannot bind due to unsupported vnic_type: %s",
                      vnic_type)
            return
        for segment in context.network.network_segments:
            if self._check_segment(segment):
                context.set_binding(segment[api.ID],
                                    portbindings.VIF_TYPE_OVS,
                                    {portbindings.CAP_PORT_FILTER: False},
                                    os_constants.PORT_STATUS_ACTIVE)

    def _validate_create_subnet(self, context, network, subnet):
        net_partition = subnet.get('net_partition')
        vsd_id = subnet.get('nuagenet')
        if vsd_id and not net_partition:
            msg = _("Parameter net-partition required when passing nuagenet")
            raise NuageBadRequest(resource='subnet', msg=msg)

        self._validate_network_segment(network)
        if subnet.get('nuagenet') and subnet.get('net_partition'):
            self._validate_create_vsd_managed_subnet(network, subnet)
            vsd_managed = True
        else:
            self._validate_create_openstack_managed_subnet(context, subnet)
            vsd_managed = False

        subnets = self.core_plugin.get_subnets(
            context,
            filters={'network_id': [subnet['network_id']]})
        subnet_ids = [s['id'] for s in subnets]
        subnet_mappings = nuagedb.get_subnet_l2doms_by_subnet_ids(
            context.session,
            subnet_ids)
        if len(set([vsd_managed] + [m['nuage_managed_subnet']
                                    for m in subnet_mappings])) > 1:
            msg = _("Can't mix openstack and vsd managed subnets under 1 "
                    "network.")
            raise NuageBadRequest(resource='subnet', msg=msg)

    def _validate_nuage_sharedresource(self, context, net_id):
        filter = {'network_id': [net_id]}
        existing_subn = self.core_plugin.get_subnets(context, filters=filter)
        if len(existing_subn) > 1:
            msg = (_('Only one subnet is allowed per external network %s')
                   % net_id)
            raise NuageBadRequest(msg=msg)

    def _validate_create_openstack_managed_subnet(self, context, subnet):

        if (lib_validators.is_attr_set(subnet['gateway_ip'])
                and netaddr.IPAddress(subnet['gateway_ip'])
                not in netaddr.IPNetwork(subnet['cidr'])):
            msg = "Gateway IP outside of the subnet CIDR "
            raise NuageBadRequest(resource='subnet', msg=msg)
        network_external = self.core_plugin._network_is_external(
            context, subnet['network_id'])
        if (not network_external and subnet['underlay'] !=
                os_constants.ATTR_NOT_SPECIFIED):
            msg = _("underlay attribute can not be set for internal subnets")
            raise NuageBadRequest(resource='subnet', msg=msg)
        if (not network_external and
                subnet['nuage_uplink']):
            msg = _("nuage-uplink attribute can not be set for "
                    "internal subnets")
            raise NuageBadRequest(resource='subnet', msg=msg)

    def _validate_create_vsd_managed_subnet(self, network, subnet):
        subnet_validate = {'net_partition': IsSet(),
                           'nuagenet': IsSet()}
        validate("subnet", subnet, subnet_validate)
        net_validate = {'router:external': Is(False)}
        validate("network", network, net_validate)

    def _validate_network_segment(self, network):
        net_type = 'provider:network_type'
        vxlan_segment = [segment for segment in network.get('segments', [])
                         if str(segment.get(net_type)).lower() == 'vxlan']
        if str(network.get(net_type)).lower() != 'vxlan' and not vxlan_segment:
            msg = _("Network should have 'provider:network_type' vxlan or have"
                    " such a segment")
            raise NuageBadRequest(msg=msg)

    def _validate_net_partition(self, subnet, db_context):
        netpartition_db = nuagedb.get_net_partition_by_name(
            db_context.session, subnet['net_partition'])
        netpartition = self.nuageclient.get_netpartition_by_name(
            subnet['net_partition'])
        require(netpartition, "netpartition", subnet['net_partition'])
        if netpartition_db:
            if netpartition_db['id'] != netpartition['id']:
                net_partdb = nuagedb.get_net_partition_with_lock(
                    db_context.session, netpartition_db['id'])
                nuagedb.delete_net_partition(db_context.session, net_partdb)
                self._add_net_partition(db_context.session, netpartition)
        else:
            self._add_net_partition(db_context.session, netpartition)
        return netpartition['id']

    def _add_net_partition(self, session, netpartition):
        return nuagedb.add_net_partition(
            session, netpartition['id'], None, None,
            netpartition['name'], None, None)

    def _get_nuage_subnet(self, nuage_subnet_id):
        nuage_subnet = self.nuageclient.get_subnet_or_domain_subnet_by_id(
            nuage_subnet_id)
        require(nuage_subnet, 'subnet or domain', nuage_subnet_id)
        shared = nuage_subnet['associatedSharedNetworkResourceID']
        shared_subnet = None
        if shared:
            shared_subnet = self.nuageclient.get_nuage_sharedresource(shared)
            require(shared_subnet, 'sharednetworkresource', shared)
            shared_subnet['subnet_id'] = shared
        return nuage_subnet, shared_subnet

    def _set_gateway_from_vsd(self, nuage_subnet, shared_subnet, subnet):
        gateway_subnet = shared_subnet or nuage_subnet
        if subnet['enable_dhcp']:
            if nuage_subnet['type'] == constants.L2DOMAIN:
                gw_ip = self.nuageclient.get_gw_from_dhcp_l2domain(
                    gateway_subnet['ID'])
            else:
                gw_ip = gateway_subnet['gateway']
            gw_ip = gw_ip or None
        else:
            gw_ip = None
            subnet['dns_nameservers'] = []
            LOG.warn("Nuage ml2 plugin will ignore dns_nameservers.")
        subnet['gateway_ip'] = gw_ip

    def _update_gw_and_pools(self, core_plugin, db_context, subnet,
                             original_gateway):
        if original_gateway == subnet['gateway_ip']:
            # The gateway from vsd is what openstack already had.
            return

        if original_gateway != subnet['gateway_ip']:
            # Gateway from vsd is different, we must recalculate the allocation
            # pools.
            new_pools = self._set_allocation_pools(core_plugin, subnet)
            core_plugin.ipam._update_subnet_allocation_pools(
                db_context, subnet['id'], {'allocation_pools': new_pools,
                                           'id': subnet['id']})
        LOG.warn("Nuage ml2 plugin will overwrite subnet gateway ip "
                 "and allocation pools")
        db_subnet = core_plugin._get_subnet(db_context, subnet['id'])
        update_subnet = {'gateway_ip': subnet['gateway_ip']}
        db_subnet.update(update_subnet)

    def _reserve_dhcp_ip(self, core_plugin, db_context, subnet, nuage_subnet,
                         shared_subnet):
        if not subnet['enable_dhcp']:
            return
        dhcp_ip = (shared_subnet['gateway']
                   if shared_subnet
                   else nuage_subnet['gateway'])
        ipam_pool = driver.NeutronDbPool(None, db_context)
        ipam_subnet = ipam_pool.get_subnet(subnet['id'])
        ipam_subnet.allocate(ipam_req.SpecificAddressRequest(dhcp_ip))

    def _set_allocation_pools(self, core_plugin, subnet):
        pools = core_plugin.ipam.generate_pools(subnet['cidr'],
                                                subnet['gateway_ip'])
        subnet['allocation_pools'] = [
            {'start': str(netaddr.IPAddress(pool.first, pool.version)),
             'end': str(netaddr.IPAddress(pool.last, pool.version))}
            for pool in pools]
        return pools

    def _cleanup_group(self, db_context, nuage_npid, nuage_subnet_id, subnet):
        try:
            if db_context.tenant == subnet['tenant_id']:
                tenants = [db_context.tenant]
            else:
                tenants = [db_context.tenant, subnet['tenant_id']]
            self.nuageclient.detach_nuage_group_to_nuagenet(
                tenants, nuage_subnet_id,
                subnet.get('shared'))
        except Exception as e:
            LOG.error("Failed to detach group from vsd subnet {tenant: %s,"
                      " netpartition: %s, vsd subnet: %s}"
                      % (db_context.tenant, nuage_npid, nuage_subnet_id))
            raise e

    def _validate_port(self, db_context, port, event):
        if 'fixed_ips' not in port or len(port.get('fixed_ips', [])) == 0:
            return False
        if port.get('device_owner') != constants.DEVICE_OWNER_IRONIC and \
                port.get('device_owner') in constants.AUTO_CREATE_PORT_OWNERS:
            return False
        if port.get(portbindings.VNIC_TYPE, portbindings.VNIC_NORMAL) \
                not in self._supported_vnic_types():
            return False
        self.nuage_callbacks.notify(resources.PORT, event,
                                    self, context=db_context,
                                    request_port=port)
        return True

    def get_subnet_mapping_by_port(self, db_context, port):
        if port['fixed_ips']:
            subnet_id = port['fixed_ips'][0]['subnet_id']
            subnet_mapping = nuagedb.get_subnet_l2dom_by_id(db_context.session,
                                                            subnet_id)
            return subnet_mapping

    def _port_should_have_vm(self, port):
        device_owner = port['device_owner']
        return (port.get('device_owner') != constants.DEVICE_OWNER_IRONIC and
                constants.NOVA_PORT_OWNER_PREF in device_owner or
                LB_DEVICE_OWNER_V2 in device_owner)

    def _create_nuage_vm(self, core_plugin, db_context, port, np_name,
                         subnet_mapping, nuage_port, nuage_subnet):
        no_of_ports, vm_id = self._get_port_num_and_vm_id_of_device(
            core_plugin, db_context, port)
        subn = core_plugin.get_subnet(
            db_context, port['fixed_ips'][0]['subnet_id'])
        params = {
            'port_id': port['id'],
            'id': vm_id,
            'mac': port['mac_address'],
            'netpart_name': np_name,
            'ip': port['fixed_ips'][0]['ip_address'],
            'no_of_ports': no_of_ports,
            'tenant': port['tenant_id'],
            'netpart_id': subnet_mapping['net_partition_id'],
            'neutron_id': port['fixed_ips'][0]['subnet_id'],
            'vport_id': nuage_port.get('ID'),
            'subn_tenant': subn['tenant_id'],
            'portOnSharedSubn': subn['shared'],
            'dhcp_enabled': subn['enable_dhcp'],
            'vsd_subnet': nuage_subnet
        }
        network_details = core_plugin.get_network(db_context,
                                                  port['network_id'])
        if network_details['shared']:
            self.nuageclient.create_usergroup(
                port['tenant_id'],
                subnet_mapping['net_partition_id'])
        return self.nuageclient.create_vms(params)

    def _get_port_num_and_vm_id_of_device(self, core_plugin, db_context, port):
        # upstream neutron_lbaas assigns a constant device_id to all the
        # lbaas_ports (which is a bug), hence we use port ID as vm_id
        # instead of device_id for lbaas dummy VM
        # as get_ports by device_id would return multiple vip_ports,
        # as workaround set no_of_ports = 1
        if port.get('device_owner') == LB_DEVICE_OWNER_V2:
            return 1, port['id']
        filters = {'device_id': [port.get('device_id')]}
        ports = core_plugin.get_ports(db_context, filters)
        ports = [p for p in ports
                 if self._is_port_vxlan_normal(p, core_plugin, db_context)]
        return len(ports), port.get('device_id')

    def _process_port_create_secgrp_for_port_sec(self, context, port):
        l2dom_id = None
        l3dom_id = None
        rtr_id = None
        policygroup_ids = []
        port_id = port['id']

        if not port.get('fixed_ips'):
            return self._make_port_dict(port)

        subnet_mapping = nuagedb.get_subnet_l2dom_by_id(
            context.session, port['fixed_ips'][0]['subnet_id'])

        if subnet_mapping:
            if subnet_mapping['nuage_l2dom_tmplt_id']:
                l2dom_id = subnet_mapping['nuage_subnet_id']
            else:
                l3dom_id = subnet_mapping['nuage_subnet_id']
                rtr_id = (self.nuageclient.
                          get_nuage_domain_id_from_subnet(l3dom_id))

            params = {
                'neutron_port_id': port_id,
                'l2dom_id': l2dom_id,
                'l3dom_id': l3dom_id,
                'rtr_id': rtr_id,
                'type': constants.VM_VPORT,
                'sg_type': constants.SOFTWARE
            }
            nuage_port = self.nuageclient.get_nuage_vport_for_port_sec(params)
            if nuage_port:
                nuage_vport_id = nuage_port.get('ID')
                if port.get(portsecurity.PORTSECURITY):
                    self.nuageclient.update_vport_policygroups(
                        nuage_vport_id, policygroup_ids)
                else:
                    sg_id = (self.nuageclient.
                             create_nuage_sec_grp_for_port_sec(params))
                    if sg_id:
                        params['sg_id'] = sg_id
                        (self.nuageclient.
                         create_nuage_sec_grp_rule_for_port_sec(params))
                        policygroup_ids.append(sg_id)
                        self.nuageclient.update_vport_policygroups(
                            nuage_vport_id, policygroup_ids)

    def _is_port_vxlan_normal(self, port, core_plugin, db_context):
        if port.get('binding:vnic_type') != portbindings.VNIC_NORMAL:
            return False

        network = core_plugin.get_network(db_context, port.get('network_id'))
        try:
            self._validate_network_segment(network)
            return True
        except Exception:
            return False

    def delete_gw_host_vport(self, context, port, subnet_mapping):
        port_params = {
            'neutron_port_id': port['id']
        }

        # Check if l2domain/subnet exist. In case of router_interface_delete,
        # subnet is deleted and then call comes to delete_port. In that
        # case, we just return
        vsd_subnet = self.nuageclient.get_subnet_or_domain_subnet_by_id(
            subnet_mapping['nuage_subnet_id'])
        if not vsd_subnet:
            return

        if subnet_mapping['nuage_managed_subnet']:
            port_params['l2dom_id'] = subnet_mapping['nuage_subnet_id']
            port_params['l3dom_id'] = subnet_mapping['nuage_subnet_id']
        else:
            if subnet_mapping['nuage_l2dom_tmplt_id']:
                port_params['l2dom_id'] = subnet_mapping['nuage_subnet_id']
            else:
                port_params['l3dom_id'] = subnet_mapping['nuage_subnet_id']
        nuage_vport = self.nuageclient.get_nuage_vport_by_neutron_id(
            port_params, required=False)
        if nuage_vport and (nuage_vport['type'] == constants.HOST_VPORT):
            def_netpart = cfg.CONF.RESTPROXY.default_net_partition_name
            netpart = nuagedb.get_default_net_partition(context, def_netpart)
            self.nuageclient.delete_nuage_gateway_vport(
                context.tenant_id,
                nuage_vport.get('ID'),
                netpart['id'])

    def _delete_nuage_vm(self, core_plugin, db_context, port, np_name,
                         subnet_mapping, is_port_device_owner_removed=False):
        no_of_ports, vm_id = self._get_port_num_and_vm_id_of_device(
            core_plugin, db_context, port)

        if is_port_device_owner_removed:
            # In case of device removed, this number should be the amount of
            # vminterfaces on VSD. If it's >1, nuagenetlib knows there are
            # still other vminterfaces using the VM, and it will not delete the
            # vm. If it's 1 or less. Nuagenetlib will also automatically delete
            # the vm. Because the port count is determined on a database count
            # of ports with device_id X, AND because the update already
            # happened by ml2plugin, AND because we're in the same database
            # transaction, the count here would return 1 less (as the updated
            # port will not be counted because the device_id is already cleared
            no_of_ports += 1

        subn = core_plugin.get_subnet(db_context, subnet_mapping['subnet_id'])
        nuage_port = self.nuageclient.get_nuage_port_by_id(
            {'neutron_port_id': port['id']})
        if not nuage_port:
            return
        params = {
            'no_of_ports': no_of_ports,
            'netpart_name': np_name,
            'tenant': port['tenant_id'],
            'nuage_vif_id': nuage_port['nuage_vif_id'],
            'id': vm_id,
            'subn_tenant': subn['tenant_id'],
            'portOnSharedSubn': subn['shared']
        }
        if not nuage_port['domainID']:
            params['l2dom_id'] = subnet_mapping['nuage_subnet_id']
        else:
            params['l3dom_id'] = subnet_mapping['nuage_subnet_id'],
        try:
            self.nuageclient.delete_vms(params)
        except Exception:
            LOG.error("Failed to delete vm from vsd {vm id: %s}"
                      % vm_id)
            raise

    def _get_nuage_vport(self, port, subnet_mapping, required=True):
        port_params = {
            'neutron_port_id': port['id'],
            'l2dom_id': subnet_mapping['nuage_subnet_id'],
            'l3dom_id': subnet_mapping['nuage_subnet_id']
        }
        return self.nuageclient.get_nuage_vport_by_neutron_id(
            port_params, required=required)

    def _check_segment(self, segment):
        network_type = segment[api.NETWORK_TYPE]
        return network_type == p_constants.TYPE_VXLAN

    def _supported_vnic_types(self):
        return [portbindings.VNIC_NORMAL]
