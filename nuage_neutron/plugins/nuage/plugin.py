# Copyright 2014 Alcatel-Lucent USA Inc.
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

import contextlib
import copy
import logging as py_logging
import netaddr
import re

from logging import handlers
from oslo.db import exception as db_exc
from oslo_concurrency import lockutils
from oslo_config import cfg
from oslo_utils import excutils
from oslo_utils import importutils
from sqlalchemy import exc as sql_exc
from sqlalchemy.orm import exc

from neutron.api import extensions as neutron_extensions
from neutron.api.v2 import attributes
from neutron.common import constants as os_constants
from neutron.common import exceptions as n_exc
from neutron.common import log
from neutron.common import utils
from neutron.db import api as db
from neutron.db import db_base_plugin_v2
from neutron.db import external_net_db
from neutron.db import extraroute_db
from neutron.db import l3_gwmode_db
from neutron.db import quota_db  # noqa
from neutron.db import securitygroups_db as sg_db
from neutron.extensions import external_net
from neutron.extensions import l3
from neutron.extensions import portbindings
from neutron.extensions import providernet as pnet
from neutron.extensions import securitygroup as ext_sg
from neutron.openstack.common import loopingcall
from neutron import policy
from nuage_neutron.plugins.nuage.common import config
from nuage_neutron.plugins.nuage.common import constants
from nuage_neutron.plugins.nuage.common import exceptions as nuage_exc
from nuage_neutron.plugins.nuage.common import utils as nuage_utils
from nuage_neutron.plugins.nuage import extensions
from nuage_neutron.plugins.nuage.extensions import netpartition
from nuage_neutron.plugins.nuage.extensions import vsd_subnet
from nuage_neutron.plugins.nuage import gateway
from nuage_neutron.plugins.nuage import nuagedb
from oslo_log import log as logging

LOG = logging.getLogger(__name__)


class NuagePlugin(db_base_plugin_v2.NeutronDbPluginV2,
                  external_net_db.External_net_db_mixin,
                  extraroute_db.ExtraRoute_db_mixin,
                  l3_gwmode_db.L3_NAT_db_mixin,
                  gateway.NuagegatewayMixin,
                  netpartition.NetPartitionPluginBase,
                  sg_db.SecurityGroupDbMixin,
                  vsd_subnet.VsdSubnetPluginBase):
    """Class that implements Nuage Networks' hybrid plugin functionality."""
    vendor_extensions = ["net-partition", "nuage-router", "nuage-subnet",
                         "ext-gw-mode", "nuage-floatingip", "vsd-subnet",
                         "nuage-gateway", "appdesigner"]

    binding_view = "extension:port_binding:view"

    def __init__(self):
        super(NuagePlugin, self).__init__()
        neutron_extensions.append_api_extensions_path(extensions.__path__)
        config.nuage_register_cfg_opts()
        self.nuageclient_init()
        self._prepare_default_netpartition()
        self.init_fip_rate_log()
        LOG.debug("NuagePlugin initialization done")

    db_base_plugin_v2.NeutronDbPluginV2.register_dict_extend_funcs(
        attributes.NETWORKS, ['_extend_network_dict_provider_nuage'])

    def nuageclient_init(self):
        server = cfg.CONF.RESTPROXY.server
        serverauth = cfg.CONF.RESTPROXY.serverauth
        serverssl = cfg.CONF.RESTPROXY.serverssl
        base_uri = cfg.CONF.RESTPROXY.base_uri
        auth_resource = cfg.CONF.RESTPROXY.auth_resource
        organization = cfg.CONF.RESTPROXY.organization
        nuageclient = importutils.import_module('nuagenetlib.nuageclient')
        self.nuageclient = nuageclient.NuageClient(server, base_uri,
                                                   serverssl, serverauth,
                                                   auth_resource,
                                                   organization)

    def init_fip_rate_log(self):
        self.def_fip_rate = cfg.CONF.FIPRATE.default_fip_rate
        self.fip_rate_log = None
        if cfg.CONF.FIPRATE.fip_rate_change_log:
            formatter = py_logging.Formatter('%(asctime)s %(levelname)s '
                                             '[%(user_name)s] %(message)s')
            self.fip_rate_log = logging.getLogger("neutron.nuage.fip.rate")
            handler = handlers.WatchedFileHandler(
                cfg.CONF.FIPRATE.fip_rate_change_log)
            handler.setFormatter(formatter)
            self.fip_rate_log.logger.addHandler(handler)
        else:
            self.fip_rate_log = LOG

    @log.log
    def _synchronization_thread(self):
        sync_interval = cfg.CONF.SYNCMANAGER.sync_interval
        fip_quota = str(cfg.CONF.RESTPROXY.default_floatingip_quota)
        if sync_interval > 0:
            args = (fip_quota, cfg.CONF.SYNCMANAGER.enable_sync)
            sync_loop = loopingcall.FixedIntervalLoopingCall(
                self.syncmanager.synchronize, *args)
            sync_loop.start(interval=sync_interval)
        else:
            self.syncmanager.synchronize(fip_quota,
                                         cfg.CONF.SYNCMANAGER.enable_sync)

    @log.log
    def _resource_finder(self, context, for_resource, resource, user_req):
        match = re.match(attributes.UUID_PATTERN, user_req[resource])
        if match:
            obj_lister = getattr(self, "get_%s" % resource)
            found_resource = obj_lister(context, user_req[resource])
            if not found_resource:
                msg = (_("%(resource)s with id %(resource_id)s does not "
                         "exist") % {'resource': resource,
                                     'resource_id': user_req[resource]})
                raise n_exc.BadRequest(resource=for_resource, msg=msg)
        else:
            filter = {'name': [user_req[resource]]}
            obj_lister = getattr(self, "get_%ss" % resource)
            found_resource = obj_lister(context, filters=filter)
            if not found_resource:
                msg = (_("Either %(resource)s %(req_resource)s not found "
                         "or you dont have credential to access it")
                       % {'resource': resource,
                          'req_resource': user_req[resource]})
                raise n_exc.BadRequest(resource=for_resource, msg=msg)
            if len(found_resource) > 1:
                msg = (_("More than one entry found for %(resource)s "
                         "%(req_resource)s. Use id instead")
                       % {'resource': resource,
                          'req_resource': user_req[resource]})
                raise n_exc.BadRequest(resource=for_resource, msg=msg)
            found_resource = found_resource[0]
        return found_resource

    @log.log
    def _create_update_port(self, context, port, np_name,
                            subnet_mapping):
        filters = {'device_id': [port['device_id']]}
        ports = self.get_ports(context, filters)
        subn = self.get_subnet(context, port['fixed_ips'][0]['subnet_id'])
        params = {
            'port_id': port['id'],
            'id': port['device_id'],
            'mac': port['mac_address'],
            'netpart_name': np_name,
            'ip': port['fixed_ips'][0]['ip_address'],
            'no_of_ports': len(ports),
            'tenant': subn['tenant_id'],
            'neutron_id': port['fixed_ips'][0]['subnet_id'],
            'attached_network': True
        }

        if subnet_mapping['nuage_managed_subnet']:
            params['parent_id'] = subnet_mapping['nuage_l2dom_tmplt_id']

        self.nuageclient.create_vms(params)

    @log.log
    def _create_nuage_port(self, context, port, np_name,
                            subnet_mapping):
        filters = {'device_id': [port['device_id']]}
        ports = self.get_ports(context, filters)
        params = {
            'port_id': port['id'],
            'id': port['device_id'],
            'mac': port['mac_address'],
            'netpart_name': np_name,
            'ip': port['fixed_ips'][0]['ip_address'],
            'no_of_ports': len(ports),
            'tenant': port['tenant_id'],
            'neutron_id': port['fixed_ips'][0]['subnet_id']
        }

        if subnet_mapping['nuage_managed_subnet']:
            params['parent_id'] = subnet_mapping['nuage_l2dom_tmplt_id']

        self.nuageclient.create_vport(params)

    @log.log
    def _update_nuage_port(self, context, port, np_name,
                            subnet_mapping, nuage_port):
        filters = {'device_id': [port['device_id']]}
        ports = self.get_ports(context, filters)
        params = {
            'port_id': port['id'],
            'id': port['device_id'],
            'mac': port['mac_address'],
            'netpart_name': np_name,
            'ip': port['fixed_ips'][0]['ip_address'],
            'no_of_ports': len(ports),
            'tenant': port['tenant_id'],
            'neutron_id': port['fixed_ips'][0]['subnet_id'],
            'vport_id': nuage_port.get('nuage_vport_id')
        }

        if subnet_mapping['nuage_managed_subnet']:
            params['parent_id'] = subnet_mapping['nuage_l2dom_tmplt_id']

        self.nuageclient.create_vms(params)

    @log.log
    def _get_router_by_subnet(self, context, subnet_id):
        filters = {
            'fixed_ips': {'subnet_id': [subnet_id]},
            'device_owner': [os_constants.DEVICE_OWNER_ROUTER_INTF]
        }
        router_port = self.get_ports(context, filters=filters)
        if not router_port:
            msg = (_("Router for subnet %s not found ") % subnet_id)
            raise n_exc.BadRequest(resource='port', msg=msg)
        return router_port[0]['device_id']

    @log.log
    def _process_port_create_security_group(self, context, port,
                                            sec_group,
                                            vport_type=constants.VM_VPORT,
                                            vport_id=None):
        if not attributes.is_attr_set(sec_group):
            port[ext_sg.SECURITYGROUPS] = []
            return
        if len(sec_group) > 1:
            msg = (_("Multiple security group on a port not supported "
                     "on nuage VSP"))
            raise nuage_exc.NuageBadRequest(msg=msg)
        port_id = port['id']
        with context.session.begin(subtransactions=True):
            for sg_id in sec_group:
                super(NuagePlugin,
                      self)._create_port_security_group_binding(context,
                                                                port_id,
                                                                sg_id)
        l2dom_id = None
        l3dom_id = None
        #Get l2dom or l3dom_id
        if not port.get('fixed_ips'):
            return self._make_port_dict(port)
        subnet_id = port['fixed_ips'][0]['subnet_id']
        subnet_mapping = nuagedb.get_subnet_l2dom_by_id(context.session,
                                                        subnet_id)
        if subnet_mapping:
            if subnet_mapping['nuage_l2dom_tmplt_id']:
                l2dom_id = subnet_mapping['nuage_subnet_id']
            else:
                l3dom_id = subnet_mapping['nuage_subnet_id']
        try:
            vptag_vport_list = []
            for sg_id in sec_group:
                params = {
                    'neutron_port_id': port_id,
                    'nuage_vport_type': vport_type,
                    'nuage_vport_id': vport_id,
                    'l2dom_id': l2dom_id,
                    'l3dom_id': l3dom_id
                }
                #To-Do: Verify gateway API feature
                nuage_port = self.nuageclient.get_nuage_vport_by_id(params)
                nuage_port['l2dom_id'] = l2dom_id
                nuage_port['l3dom_id'] = l3dom_id
                if nuage_port and nuage_port.get('nuage_vport_id'):
                    nuage_vport_id = nuage_port['nuage_vport_id']
                    sg = self._get_security_group(context, sg_id)
                    sg_rules = self.get_security_group_rules(
                                        context,
                                        {'security_group_id': [sg_id]})
                    sg_params = {
                        'nuage_port': nuage_port,
                        'sg': sg,
                        'sg_rules': sg_rules
                    }
                    nuage_vptag_id = (
                        self.nuageclient.process_port_create_security_group(
                                                                    sg_params))
                    vptag_vport = {
                        'nuage_vporttag_id': nuage_vptag_id
                    }
                    vptag_vport_list.append(vptag_vport)

            if vptag_vport_list:
                params = {
                    'vptag_vport_list': vptag_vport_list,
                    'nuage_vport_id': nuage_vport_id
                }
                self.nuageclient.update_nuage_vport(params)
        except Exception:
            with excutils.save_and_reraise_exception():
                for sg_id in sec_group:
                    super(NuagePlugin,
                          self)._delete_port_security_group_bindings(context,
                                                                 port_id)
        # Convert to list as a set might be passed here and
        # this has to be serialized
        port[ext_sg.SECURITYGROUPS] = (list(sec_group) if sec_group else [])

    @log.log
    def _delete_port_security_group_bindings(self, context, port_id):
        super(NuagePlugin,
              self)._delete_port_security_group_bindings(context, port_id)
        port = self.get_port(context, port_id)
        if port.get('device_owner') not in constants.AUTO_CREATE_PORT_OWNERS:
            subnet_id = port['fixed_ips'][0]['subnet_id']
            subnet_mapping = nuagedb.get_subnet_l2dom_by_id(context.session,
                                                            subnet_id)
            if subnet_mapping:
                l2dom_id = None
                l3dom_id = None
                if subnet_mapping['nuage_l2dom_tmplt_id']:
                    l2dom_id = subnet_mapping['nuage_subnet_id']
                else:
                    l3dom_id = subnet_mapping['nuage_subnet_id']
                params = {
                    'neutron_port_id': port_id,
                    'l2dom_id': l2dom_id,
                    'l3dom_id': l3dom_id
                }
                self.nuageclient.delete_port_security_group_bindings(params)

    @lockutils.synchronized('create_port', 'nuage-port', external=True)
    @nuage_utils.handle_nuage_api_error
    @log.log
    def create_port(self, context, port):
        session = context.session
        with session.begin(subtransactions=True):
            p = port['port']
            self._ensure_default_security_group_on_port(context, port)
            port = super(NuagePlugin, self).create_port(context, port)
            device_owner = port.get('device_owner', None)
            if device_owner not in constants.AUTO_CREATE_PORT_OWNERS:
                if 'fixed_ips' not in port or len(port['fixed_ips']) == 0:
                    return self._extend_port_dict_binding(context, port)
                subnet_id = port['fixed_ips'][0]['subnet_id']
                subnet_mapping = nuagedb.get_subnet_l2dom_by_id(session,
                                                                subnet_id)
                port_prefix = constants.NOVA_PORT_OWNER_PREF
                if subnet_mapping:
                    LOG.debug("Found subnet mapping for neutron subnet %s",
                              subnet_id)

                    if port['device_owner'].startswith(port_prefix):
                        #This request is coming from nova
                        try:
                            net_partition = nuagedb.get_net_partition_by_id(
                                session,
                                subnet_mapping['net_partition_id'])
                            self._create_update_port(
                                context,
                                port,
                                net_partition['name'],
                                subnet_mapping)
                        except Exception:
                            with excutils.save_and_reraise_exception():
                                super(NuagePlugin, self).delete_port(
                                    context,
                                    port['id'])
                    else:
                        #This request is port-create no special ports
                        try:
                            net_partition = nuagedb.get_net_partition_by_id(
                                session,
                                subnet_mapping['net_partition_id'])
                            self._create_nuage_port(
                                context,
                                port,
                                net_partition['name'],
                                subnet_mapping)
                        except Exception:
                            with excutils.save_and_reraise_exception():
                                super(NuagePlugin, self).delete_port(
                                    context,
                                    port['id'])
                    if (subnet_mapping['nuage_managed_subnet'] is False
                        and ext_sg.SECURITYGROUPS in p):
                        self._process_port_create_security_group(
                            context,
                            port,
                            p[ext_sg.SECURITYGROUPS])
                        LOG.debug("Created security group for port %s",
                                  port['id'])

                    elif (subnet_mapping['nuage_managed_subnet'] and
                          ext_sg.SECURITYGROUPS in p):
                        LOG.warning(_("Security Groups is ignored for ports on"
                                      " VSD Managed Subnet"))
                else:
                    if port['device_owner'].startswith(port_prefix):
                        # VM is getting spawned on a subnet type which
                        # is not supported by VSD. LOG error.
                        LOG.error(_('VM with uuid %s will not be resolved '
                                  'in VSD because its created on unsupported'
                                  'subnet type'), port['device_id'])
        return self._extend_port_dict_binding(context, port)

    @nuage_utils.handle_nuage_api_error
    @log.log
    def update_port(self, context, id, port):
        p = port['port']
        sg_groups = None
        session = context.session
        with session.begin(subtransactions=True):
            original_port = self.get_port(context, id)
            changed_owner = p.get('device_owner')
            current_owner = original_port['device_owner']

            if p.get('device_owner', '').startswith(constants.NOVA_PORT_OWNER_PREF):
                LOG.debug("Port %s is owned by nova:compute", id)
                port = self._get_port(context, id)

                if current_owner == constants.APPD_PORT:
                    p['device_owner'] = constants.APPD_PORT
                port.update(p)

                if not port.get('fixed_ips'):
                    return self._make_port_dict(port)

                subnet_id = port['fixed_ips'][0]['subnet_id']
                subnet_mapping = nuagedb.get_subnet_l2dom_by_id(session,
                                                                subnet_id)
                if subnet_mapping:
                    l2dom_id = None
                    l3dom_id = None
                    if subnet_mapping['nuage_l2dom_tmplt_id']:
                        l2dom_id = subnet_mapping['nuage_subnet_id']
                    else:
                        l3dom_id = subnet_mapping['nuage_subnet_id']
                    params = {
                        'neutron_port_id': id,
                        'l2dom_id': l2dom_id,
                        'l3dom_id': l3dom_id
                    }
                    nuage_port = self.nuageclient.get_nuage_vport_by_id(params)
                    if nuage_port:
                        net_partition = nuagedb.get_net_partition_by_id(
                            session, subnet_mapping['net_partition_id'])
                        self._update_nuage_port(context, port,
                                                 net_partition['name'],
                                                 subnet_mapping, nuage_port)
                    else:
                        #should not come here, log debug message
                        LOG.debug("Nuage vport does not exist for port %s ",id)
                else:
                    LOG.error(_('VM with uuid %s will not be resolved '
                              'in VSD because its created on unsupported'
                              'subnet type'), port['device_id'])

                self._check_floatingip_update(context, port)
                updated_port = self._make_port_dict(port)
                if subnet_mapping['nuage_managed_subnet'] is False:
                    sg_port = self._extend_port_dict_security_group(
                        updated_port,
                        port)
                    sg_groups = sg_port[ext_sg.SECURITYGROUPS]
            else:
                LOG.debug("Port %s is not owned by nova:compute", id)
                if current_owner == constants.APPD_PORT:
                    p['device_owner'] = constants.APPD_PORT
                    port.update(p)
                filters = {'device_id': [original_port['device_id']]}
                ports = self.get_ports(context, filters)
                #nova removes device_owner and device_id fields, in this
                #update_port, hence before update_port, get_ports for
                #device_id and pass the no_of_ports to delete_nuage_vport
                no_of_ports = len(ports)
                updated_port = super(NuagePlugin,
                    self).update_port(context, id,
                                      port)
                if not updated_port.get('fixed_ips'):
                    return updated_port

                subnet_id = updated_port['fixed_ips'][0]['subnet_id']
                subnet_mapping = nuagedb.get_subnet_l2dom_by_id(
                    context.session, subnet_id)
                # nova delete has removed the compute:none from device_owner
                # Delete the nuage_vport on VSD when the device_owner is appd
                # and only when the call is nova delete. When it is neutron
                # port update, we cannot receive binding:host_id in the
                # dict since this is not an updatable attribute.
                if ((not changed_owner and current_owner.startswith(
                        constants.NOVA_PORT_OWNER_PREF)) or
                        ((current_owner == constants.APPD_PORT and
                        'binding:host_id' in p))):
                    LOG.debug("nova:compute onwership removed for port %s ",
                               id)
                    if subnet_mapping:
                        net_partition = nuagedb.get_net_partition_by_id(
                                session, subnet_mapping['net_partition_id'])
                        # delete nuage_vm
                        self._delete_nuage_vport(context, original_port,
                                                 net_partition['name'],
                                                 subnet_mapping, no_of_ports)

        if (subnet_mapping and
            subnet_mapping['nuage_managed_subnet'] is False):
            if sg_groups:
                self._delete_port_security_group_bindings(
                    context, updated_port['id'])
                self._process_port_create_security_group(context,
                                                         updated_port,
                                                         sg_groups)
                LOG.debug("Updated security-groups on port %s", id)
            elif ext_sg.SECURITYGROUPS in p:
                self._delete_port_security_group_bindings(
                    context, updated_port['id'])
                self._process_port_create_security_group(
                    context,
                    updated_port,
                    p[ext_sg.SECURITYGROUPS]
                )
        elif (subnet_mapping and subnet_mapping['nuage_managed_subnet']):
            if sg_groups or (ext_sg.SECURITYGROUPS in p):
                LOG.warning(_("Security Groups is ignored for ports on "
                              "VSD Managed Subnet"))
        return updated_port

    @log.log
    def _delete_nuage_vport(self, context, port, np_name, subnet_mapping,
                            no_of_ports=None):
        nuage_vif_id = None
        l2dom_id = None
        l3dom_id = None
        if subnet_mapping['nuage_l2dom_tmplt_id']:
            l2dom_id = subnet_mapping['nuage_subnet_id']
        else:
            l3dom_id = subnet_mapping['nuage_subnet_id']
        port_params = {
            'neutron_port_id': port['id'],
            'l2dom_id': l2dom_id,
            'l3dom_id': l3dom_id
        }
        subn = self.get_subnet(context, port['fixed_ips'][0]['subnet_id'])
        nuage_port = self.nuageclient.get_nuage_port_by_id(port_params)
        if (constants.NOVA_PORT_OWNER_PREF in port['device_owner'] or
                    port['device_owner'] == constants.APPD_PORT):
            LOG.debug("Deleting VM port %s", port['id'])
            # This was a VM Port
            if nuage_port:
                nuage_vif_id = nuage_port['nuage_vif_id']
            # no_of_ports not passed in case of nova vm create
            # without using preexisting ports
            if not no_of_ports:
                filters = {'device_id': [port['device_id']]}
                ports = self.get_ports(context, filters)
                no_of_ports = len(ports)
            params = {
                'no_of_ports': no_of_ports,
                'netpart_name': np_name,
                'tenant': subn['tenant_id'],
                'mac': port['mac_address'],
                'nuage_vif_id': nuage_vif_id,
                'id': port['device_id']
            }
            self.nuageclient.delete_vms(params)

        # delete nuage vport
        if not nuage_port and (port.get('device_owner') not in
            constants.AUTO_CREATE_PORT_OWNERS):
            nuage_vport = self.nuageclient.get_nuage_vport_by_id(port_params)
            if nuage_vport:
                self.nuageclient.delete_nuage_vport(
                        nuage_vport.get('nuage_vport_id'))

    @lockutils.synchronized('delete-port', 'nuage-del', external=True)
    @nuage_utils.handle_nuage_api_error
    @log.log
    def delete_port(self, context, id, l3_port_check=True):
        if l3_port_check:
            self.prevent_l3_port_deletion(context, id)
        port = self._get_port(context, id)
        # This is required for to pass ut test_floatingip_port_delete
        self.disassociate_floatingips(context, id)
        if not port['fixed_ips']:
            return super(NuagePlugin, self).delete_port(context, id)

        sub_id = port['fixed_ips'][0]['subnet_id']

        subnet_mapping = nuagedb.get_subnet_l2dom_by_id(context.session,
                                                        sub_id)
        if not subnet_mapping:
            LOG.debug("No subnet to l2domain mapping found for subnet %s",
                      sub_id)
            return super(NuagePlugin, self).delete_port(context, id)

        # Need to call this explicitly to delete vport to vporttag binding
        if (ext_sg.SECURITYGROUPS in port and
            subnet_mapping['nuage_managed_subnet'] is False):
            self._delete_port_security_group_bindings(context, id)

        netpart_id = subnet_mapping['net_partition_id']
        net_partition = nuagedb.get_net_partition_by_id(context.session,
                                                        netpart_id)

        if port['device_owner'] == constants.APPD_PORT:
            match = re.match(attributes.UUID_PATTERN, port['device_id'])
            if match:
                msg = (_("port with ID %s has a VM with ID %s attached to it")
                         % (port['id'], port['device_id']))
                raise nuage_exc.NuageBadRequest(msg=msg)
            else:
                super(NuagePlugin, self).delete_port(context, id)
        else:
            self._delete_nuage_vport(context, port, net_partition['name'],
                                     subnet_mapping)
            super(NuagePlugin, self).delete_port(context, id)

    @log.log
    def _check_view_auth(self, context, resource, action):
        return policy.check(context, action, resource)

    @log.log
    def _extend_port_dict_binding(self, context, port):
        if self._check_view_auth(context, port, self.binding_view):
            port[portbindings.VIF_TYPE] = portbindings.VIF_TYPE_OVS
            port[portbindings.VIF_DETAILS] = {
                portbindings.CAP_PORT_FILTER: False
            }
        return port

    @log.log
    def get_port(self, context, id, fields=None):
        port = super(NuagePlugin, self).get_port(context, id, fields)
        return self._fields(self._extend_port_dict_binding(context, port),
                            fields)

    @log.log
    def get_ports(self, context, filters=None, fields=None):
        ports = super(NuagePlugin, self).get_ports(context,
                                                   filters, fields)
        return [self._fields(self._extend_port_dict_binding(context, port),
                             fields) for port in ports]

    @log.log
    def _check_router_subnet_for_tenant(self, context, tenant_id):
        # Search router and subnet tables.
        # If no entry left delete user and group from VSD
        filters = {'tenant_id': [tenant_id]}
        routers = self.get_routers(context, filters=filters)
        subnets = self.get_subnets(context, filters=filters)
        return bool(routers or subnets)

    @log.log
    def _extend_network_dict_provider_nuage(self, network, net_db,
                                            net_binding=None):
        binding = net_db.pnetbinding if net_db else net_binding
        if binding:
            network[pnet.NETWORK_TYPE] = binding.network_type
            network[pnet.PHYSICAL_NETWORK] = binding.physical_network
            network[pnet.SEGMENTATION_ID] = binding.vlan_id

    @log.log
    def _process_provider_create(self, context, attrs):
        network_type = attrs.get(pnet.NETWORK_TYPE)
        physical_network = attrs.get(pnet.PHYSICAL_NETWORK)
        segmentation_id = attrs.get(pnet.SEGMENTATION_ID)

        network_type_set = attributes.is_attr_set(network_type)
        physical_network_set = attributes.is_attr_set(physical_network)
        segmentation_id_set = attributes.is_attr_set(segmentation_id)

        if not (network_type_set or physical_network_set or
                segmentation_id_set):
            return None, None, None
        if attrs.get(external_net.EXTERNAL):
            msg = _("provider network with external=True is not "
                    "supported in VSP")
            raise nuage_exc.NuageBadRequest(msg=msg)
        if not network_type_set:
            msg = _("provider:network_type required")
            raise n_exc.InvalidInput(error_message=msg)
        elif network_type != 'vlan':
            msg = (_("provider:network_type %s not supported in VSP")
                   % network_type)
            raise nuage_exc.NuageBadRequest(msg=msg)
        if not physical_network_set:
            msg = _("provider:physical_network required")
            raise nuage_exc.NuageBadRequest(msg=msg)
        if not segmentation_id_set:
            msg = _("provider:segmentation_id required")
            raise nuage_exc.NuageBadRequest(msg=msg)

        self.nuageclient.validate_provider_network(network_type,
                                                   physical_network,
                                                   segmentation_id)

        return network_type, physical_network, segmentation_id

    @nuage_utils.handle_nuage_api_error
    @log.log
    def create_network(self, context, network):
        binding = None
        (network_type, physical_network,
         vlan_id) = self._process_provider_create(context,
                                                  network['network'])
        with context.session.begin(subtransactions=True):
            self._ensure_default_security_group(
                context,
                network['network']['tenant_id']
            )
            net = super(NuagePlugin, self).create_network(context,
                                                          network)
            self._process_l3_create(context, net, network['network'])
            if network_type == 'vlan':
                binding = nuagedb.add_network_binding(context.session,
                                            net['id'],
                                            network_type,
                                            physical_network, vlan_id)
            self._extend_network_dict_provider_nuage(net, None, binding)
        return net

    @log.log
    def _validate_update_network(self, context, id, network):
        subnets = self._get_subnets_by_network(context, id)
        for subn in subnets:
            subnet_l2dom = nuagedb.get_subnet_l2dom_by_id(
                    context.session, subn['id'])
            if subnet_l2dom and subnet_l2dom.get('nuage_managed_subnet'):
                msg = _('Network %s has a VSD-Managed subnet associated'
                        ' with it') % id
                raise nuage_exc.OperationNotSupported(msg=msg)

        req_data = network['network']
        is_external_set = req_data.get(external_net.EXTERNAL)
        if not attributes.is_attr_set(is_external_set):
            return (None, None)
        neutron_net = self.get_network(context, id)
        if neutron_net.get(external_net.EXTERNAL) == is_external_set:
            return (None, None)
        subnet = self._validate_nuage_sharedresource(context, 'network', id)
        if subnet and not is_external_set:
            msg = _('External network with subnets can not be '
                    'changed to non-external network')
            raise nuage_exc.OperationNotSupported(msg=msg)
        if is_external_set:
            # Check if there are vm ports attached to this network
            # If there are, then updating the network is not allowed
            ports = self.get_ports(context, filters={'network_id': [id]})
            for p in ports:
                if p['device_owner'].startswith(
                        constants.NOVA_PORT_OWNER_PREF):
                    raise n_exc.NetworkInUse(net_id=id)
        return (is_external_set, subnet)

    @nuage_utils.handle_nuage_api_error
    @log.log
    def update_network(self, context, id, network):
        pnet._raise_if_updates_provider_attributes(network['network'])
        with context.session.begin(subtransactions=True):
            is_external_set, subnet = self._validate_update_network(context,
                                                                    id,
                                                                    network)
            net = super(NuagePlugin, self).update_network(context, id,
                                                          network)
            self._process_l3_update(context, net, network['network'])
            if subnet and is_external_set:
                subn = subnet[0]
                subnet_l2dom = nuagedb.get_subnet_l2dom_by_id(context.session,
                                                              subn['id'])
                if subnet_l2dom:
                    user_id = subnet_l2dom['nuage_user_id']
                    group_id = subnet_l2dom['nuage_group_id']
                    LOG.debug("Found subnet %(subn_id)s to l2 domain mapping"
                              " %(nuage_subn_id)s",
                              {'subn_id': subn['id'],
                               'nuage_subn_id': (subnet_l2dom[
                                                       'nuage_subnet_id'])})
                    self.nuageclient.delete_subnet(subn['id'])
                    nuagedb.delete_subnetl2dom_mapping(context.session,
                                                       subnet_l2dom)
                    if not self._check_router_subnet_for_tenant(
                            context, subn['tenant_id']):
                        LOG.debug("No router/subnet found for tenant %s",
                                  subn['tenant_id'])
                        self.nuageclient.delete_user(user_id)
                        self.nuageclient.delete_group(group_id)

                    self._add_nuage_sharedresource(subnet[0],
                                                   id,
                                                   constants.SR_TYPE_FLOATING)
        return net

    @nuage_utils.handle_nuage_api_error
    @log.log
    def delete_network(self, context, id):
        with context.session.begin(subtransactions=True):
            self._process_l3_delete(context, id)
            filter = {'network_id': [id]}
            subnets = self.get_subnets(context, filters=filter)
            for subnet in subnets:
                LOG.debug("Deleting subnet %s", subnet['id'])
                self.delete_subnet(context, subnet['id'])
            LOG.debug('Deleting network %s', id)
            super(NuagePlugin, self).delete_network(context, id)

    @log.log
    def _get_net_partition_for_subnet(self, context, subnet):
        ent = subnet.get('net_partition', None)
        if not ent:
            def_net_part = cfg.CONF.RESTPROXY.default_net_partition_name
            net_partition = nuagedb.get_net_partition_by_name(context.session,
                                                              def_net_part)
        else:
            net_partition = self._resource_finder(context, 'subnet',
                                                  'net_partition', subnet)
        if not net_partition:
            msg = _('Either net_partition is not provided with subnet OR '
                    'default net_partition is not created at the start')
            raise n_exc.BadRequest(resource='subnet', msg=msg)
        return net_partition

    @log.log
    def _validate_create_subnet(self, context, subnet, network_external):
        subnets = self._get_subnets_by_network(context, subnet['network_id'])
        subnet_nuagenet = subnet.get('nuagenet')
        # do not allow os_managed subnets if the network already has
        # vsd_managed subnets. and not allow vsd_managed subnets if the
        #network already has os_managed subnets
        for subn in subnets:
            subnet_l2dom = nuagedb.get_subnet_l2dom_by_id(
                    context.session, subn['id'])
            if subnet_l2dom:
                # vsd managed subnet
                if subnet_l2dom.get('nuage_managed_subnet'):
                    if not subnet_nuagenet:
                        msg = _('Network has vsd managed subnets,'
                                ' cannot create '
                                'os managed subnets')
                        raise nuage_exc.NuageBadRequest(msg=msg)
                else:
                    if subnet_nuagenet:
                        msg = _('Network has os managed subnets,'
                                ' cannot create '
                                'vsd managed subnets')
                        raise nuage_exc.NuageBadRequest(msg=msg)

        if (attributes.is_attr_set(subnet['gateway_ip'])
            and netaddr.IPAddress(subnet['gateway_ip'])
            not in netaddr.IPNetwork(subnet['cidr'])):
            msg = "Gateway IP outside of the subnet CIDR "
            raise nuage_exc.NuageBadRequest(msg=msg)

        if (not network_external and
            subnet['underlay'] != attributes.ATTR_NOT_SPECIFIED):
            msg = _("underlay attribute can not be set for internal subnets")
            raise nuage_exc.NuageBadRequest(msg=msg)

    @log.log
    def _validate_create_provider_subnet(self, context, net_id):
        net_filter = {'network_id': [net_id]}
        existing_subn = self.get_subnets(context, filters=net_filter)
        if len(existing_subn) > 0:
            msg = _('Only one subnet is allowed per '
                    'Provider network %s') % net_id
            raise nuage_exc.OperationNotSupported(msg=msg)

    @log.log
    def _delete_nuage_sharedresource(self, net_id):
        self.nuageclient.delete_nuage_sharedresource(net_id)

    @log.log
    def _validate_nuage_sharedresource(self, context, resource, net_id):
        filter = {'network_id': [net_id]}
        existing_subn = self.get_subnets(context, filters=filter)
        if len(existing_subn) > 1:
            msg = _('Only one subnet is allowed per '
                    'external network %s') % net_id
            raise nuage_exc.OperationNotSupported(msg=msg)
        return existing_subn

    @log.log
    def _add_nuage_sharedresource(self, subnet, net_id, type,
                                  req_subnet=None):
        net = netaddr.IPNetwork(subnet['cidr'])
        params = {
            'neutron_subnet': subnet,
            'net': net,
            'type': type,
            'net_id': net_id
        }
        params['underlay_config'] = cfg.CONF.RESTPROXY.nuage_fip_underlay
        if (req_subnet and req_subnet.get('underlay') in [True, False]):
            params['underlay'] = req_subnet.get('underlay')
        self.nuageclient.create_nuage_sharedresource(params)

    @log.log
    def _create_nuage_sharedresource(self, context, subnet, type):
        req_subnet = copy.deepcopy(subnet['subnet'])
        net_id = req_subnet['network_id']
        self._validate_nuage_sharedresource(context, 'subnet', net_id)
        with context.session.begin(subtransactions=True):
            neutron_subnet = super(NuagePlugin, self).create_subnet(context,
                                                                    subnet)
            self._add_nuage_sharedresource(neutron_subnet,
                                           net_id, type,req_subnet=req_subnet)
            return neutron_subnet

    @log.log
    def _create_port_gateway(self, context, subnet, gw_ip=None):
        if gw_ip is not None:
            fixed_ip = [{'ip_address': gw_ip, 'subnet_id': subnet['id']}]
        else:
            fixed_ip = [{'subnet_id': subnet['id']}]

        port_dict = dict(port=dict(
            name='',
            device_id='',
            admin_state_up=True,
            network_id=subnet['network_id'],
            tenant_id=subnet['tenant_id'],
            fixed_ips=fixed_ip,
            mac_address=attributes.ATTR_NOT_SPECIFIED,
            device_owner=constants.DEVICE_OWNER_DHCP_NUAGE))
        port = super(NuagePlugin, self).create_port(context, port_dict)
        return port

    @log.log
    def _delete_port_gateway(self, context, ports):
        for port in ports:
            super(NuagePlugin, self).delete_port(context, port['id'])

    @log.log
    def _create_nuage_subnet(self, context, neutron_subnet, netpart_id,
                             pnet_binding):
        gw_port = None
        neutron_net = self.get_network(context,
                                       neutron_subnet['network_id'])
        net = netaddr.IPNetwork(neutron_subnet['cidr'])
        # list(net)[-1] is the broadcast

        params = {
            'netpart_id': netpart_id,
            'tenant_id': neutron_subnet['tenant_id'],
            'net': net,
            'pnet_binding': pnet_binding,
            'shared': neutron_net['shared']
        }

        if neutron_subnet.get('enable_dhcp'):
            last_address = neutron_subnet['allocation_pools'][-1]['end']
            gw_port = self._create_port_gateway(context, neutron_subnet,
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
                    self._delete_port_gateway(context, [gw_port])
                super(NuagePlugin, self).delete_subnet(context,
                                                       neutron_subnet['id'])

        if nuage_subnet:
            l2dom_id = str(nuage_subnet['nuage_l2template_id'])
            user_id = nuage_subnet['nuage_userid']
            group_id = nuage_subnet['nuage_groupid']
            id = nuage_subnet['nuage_l2domain_id']
            with context.session.begin(subtransactions=True):
                nuagedb.add_subnetl2dom_mapping(context.session,
                                                neutron_subnet['id'],
                                                id,
                                                netpart_id,
                                                l2dom_id=l2dom_id,
                                                nuage_user_id=user_id,
                                                nuage_group_id=group_id)

    @log.log
    def _validate_adv_subnet(self, context, subn, nuage_netpart):
        net_id = subn['network_id']
        nuage_subn_id = subn['nuagenet']
        pnet_binding = nuagedb.get_network_binding(context.session, net_id)
        network_external = self._network_is_external(context, net_id)

        if pnet_binding:
            msg = (_("VSD-Managed Subnet create not allowed on provider "
                         "network"))
            raise nuage_exc.NuageBadRequest(msg=msg)

        if network_external:
            msg = (_("VSD-Managed Subnet create not allowed on external "
                         "network"))
            raise nuage_exc.NuageBadRequest(msg=msg)

        if nuage_netpart is None:
            msg = ("Provided net-partition does not match VSD "
                   "configuration. ")
            raise n_exc.BadRequest(resource='subnet', msg=msg)

        if not self.nuageclient.ckeck_if_l2Dom_in_correct_ent(nuage_subn_id,
                                                              nuage_netpart):
            msg = ("Provided Nuage subnet not in the provided"
                   " Nuage net-partition")
            raise n_exc.BadRequest(resource='subnet', msg=msg)

        if nuagedb.get_subnet_l2dom_by_nuage_id(
                context.session, nuage_subn_id):
            msg = ("Multiple Openstack subnets cannot be linked to the "
                   "same VSD network")
            raise n_exc.BadRequest(resource='subnet', msg=msg)

        try:
            nuage_ip, nuage_netmask = self.nuageclient.get_nuage_cidr(
                nuage_subn_id)
        except Exception:
            msg = ("Provided nuagenet ID does not match VSD "
                   "configuration. ")
            raise n_exc.BadRequest(resource='subnet', msg=msg)
        else:
            if nuage_ip:
                if not subn['enable_dhcp']:
                    msg = "DHCP must be enabled for this subnet"
                    raise n_exc.BadRequest(resource='subnet', msg=msg)
                cidr = netaddr.IPNetwork(subn['cidr'])
                if (nuage_ip != str(cidr.ip) or
                    nuage_netmask != str(cidr.netmask)):
                    msg = ("Provided IP configuration does not match VSD "
                           "configuration")
                    raise n_exc.BadRequest(resource='subnet', msg=msg)
            else:
            # this is the case for VSD-Managed unmanaged subnet
                if subn['enable_dhcp']:
                    msg = "DHCP must be disabled for this subnet"
                    raise n_exc.BadRequest(resource='subnet', msg=msg)

    @log.log
    def _get_gwip_for_adv_managed_subn(self, subn):
        gw_ip_from_cli = subn['gateway_ip']
        nuage_subn_id = subn['nuagenet']

        # case for adv. managed subnet
        # return the gw_ip with which the dhcp port is created
        # in case of adv. managed subnets
        (gw_ip_via_dhcp_options,
        gw_ip, is_l3) = self.nuageclient.get_gateway_ip_for_advsub(
                        nuage_subn_id)

        if is_l3:
            subn['gateway_ip'] = gw_ip
        else:
            # case for l2 only domain VSD-Managed subnets
            if gw_ip_via_dhcp_options:
                subn['gateway_ip'] = gw_ip_via_dhcp_options
            else:
                subn['gateway_ip'] = None

        if attributes.is_attr_set(gw_ip_from_cli):
            if gw_ip_from_cli != subn['gateway_ip']:
                msg = ("Provided gateway-ip does not match VSD "
                       "configuration. ")
                raise n_exc.BadRequest(resource='subnet', msg=msg)
        if attributes.is_attr_set(subn['dns_nameservers']):
            LOG.warning(_("DNS Nameservers parameter ignored for "
                          "VSD-Managed managed subnet "))
        # creating a dhcp_port with this gatewayIP
        return gw_ip

    @log.log
    def _link_nuage_adv_subnet(self, context, subnet):
        subn = subnet['subnet']
        nuage_subn_id = subn['nuagenet']
        nuage_tmplt_id = nuage_subn_id
        gw_ip = None

        nuage_netpart_name = subn.get('net_partition', None)

        if not nuage_netpart_name:
            msg = 'In advance mode, net-partition name must be provided'
            raise n_exc.BadRequest(resource='subnet', msg=msg)

        nuage_netpart = nuagedb.get_net_partition_by_name(context.session,
                                                          nuage_netpart_name)

        self._validate_adv_subnet(context, subn, nuage_netpart)

        if subn['enable_dhcp']:
            gw_ip = self._get_gwip_for_adv_managed_subn(subn)
        else:
            LOG.warning(_("CIDR parameter ignored for unmanaged subnet "))
            LOG.warning(_("Allocation Pool parameter ignored for"
                          " unmanaged subnet "))
            if attributes.is_attr_set(subn['gateway_ip']):
                subn['gateway_ip'] = None
                LOG.warning(_("Gateway IP parameter ignored for "
                              "VSD-Managed unmanaged subnet "))
            if attributes.is_attr_set(subn['dns_nameservers']):
                subn['dns_nameservers'] = None
                LOG.warning(_("DNS Nameservers parameter ignored "
                              "for VSD-Managed unmanaged subnet "))

        try:
            with contextlib.nested(lockutils.lock('db-access'),
                context.session.begin(subtransactions=True)):
                neutron_subnet = super(NuagePlugin, self).create_subnet(
                    context, subnet)
                if subn['enable_dhcp']:
                    # Create the dhcp port only for adv. managed subnets
                    # this the dhcp port which is being created for the
                    # adv subnet
                    # l2 case: this is the dhcp server IP seen in VSD GUI
                    # l3 case: we just create a dhcp server
                    # IP port since we need to block a VM creation with
                    # the gwIp in this domain/subnet
                    self._create_port_gateway(context, neutron_subnet, gw_ip)

                subnet_l2dom = nuagedb.add_subnetl2dom_mapping(
                    context.session, neutron_subnet['id'],
                    nuage_subn_id, nuage_netpart['id'],
                    l2dom_id=str(nuage_tmplt_id), managed=True)
        except db_exc.DBError as e:
            if isinstance(e.inner_exception, sql_exc.IntegrityError):
                msg = _("A concurrent binding to the same VSD managed"
                        " subnet detected. This operation is not allowed.")
                raise n_exc.BadRequest(resource='subnet', msg=msg)

        try:
            nuage_npid = nuage_netpart['id']
            (nuage_uid,
             nuage_gid) = self.nuageclient.attach_nuage_group_to_nuagenet(
                 context.tenant, nuage_npid, nuage_subn_id,
                 neutron_subnet.get('shared'))
        except Exception:
            filters = {
                'fixed_ips': {'subnet_id': [neutron_subnet['id']]},
                'device_owner': [constants.DEVICE_OWNER_DHCP_NUAGE]
            }
            gw_ports = self.get_ports(context, filters=filters)
            self._delete_port_gateway(context, gw_ports)
            super(NuagePlugin, self).delete_subnet(context,
                                                   neutron_subnet['id'])
            msg = "Communication with Nuage VSD failed"
            raise n_exc.BadRequest(resource='subnet', msg=msg)

        ns_dict = {}
        ns_dict['nuage_user_id'] = nuage_uid
        ns_dict['nuage_group_id'] = nuage_gid
        with context.session.begin(subtransactions=True):
            nuagedb.update_subnetl2dom_mapping(subnet_l2dom,
                                               ns_dict)
        return neutron_subnet

    @log.log
    def get_subnet(self, context, id, fields=None):
        subnet = super(NuagePlugin, self).get_subnet(context, id, None)
        subnet = nuagedb.get_nuage_subnet_info(context.session, subnet, fields)

        return self._fields(subnet, fields)


    @log.log
    def get_subnets(self, context, filters=None, fields=None, sorts=None,
                    limit=None, marker=None, page_reverse=False):
        subnets = super(NuagePlugin, self).get_subnets(context, filters, None,
                                                       sorts, limit, marker,
                                                       page_reverse)
        subnets = nuagedb.get_nuage_subnets_info(context.session, subnets,
                                                 fields, filters)
        for idx, subnet in enumerate(subnets):
            subnets[idx] = self._fields(subnet, fields)
        return subnets

    @nuage_utils.handle_nuage_api_error
    @log.log
    def create_subnet(self, context, subnet):
        subn = subnet['subnet']
        net_id = subn['network_id']

        pnet_binding = nuagedb.get_network_binding(context.session, net_id)
        network_external = self._network_is_external(context, net_id)
        self._validate_create_subnet(context, subn, network_external)

        if subn.get('nuagenet', None):
            return self._link_nuage_adv_subnet(context, subnet)

        if network_external:
            return self._create_nuage_sharedresource(
                context, subnet, constants.SR_TYPE_FLOATING)

        if pnet_binding:
            self._validate_create_provider_subnet(context, net_id)

        net_partition = self._get_net_partition_for_subnet(context, subn)
        neutron_subnet = super(NuagePlugin, self).create_subnet(context,
                                                                subnet)
        self._create_nuage_subnet(context, neutron_subnet, net_partition['id'],
                                  pnet_binding)
        return neutron_subnet

    @nuage_utils.handle_nuage_api_error
    @log.log
    def update_subnet(self, context, id, subnet):
        subn = copy.deepcopy(subnet['subnet'])
        subnet_l2dom = nuagedb.get_subnet_l2dom_by_id(context.session,
                                                      id)
        if subnet_l2dom['nuage_managed_subnet']:
            msg = ("Subnet %s is a VSD-Managed subnet."
                    " Update is not supported." % subnet_l2dom['subnet_id'])
            raise n_exc.BadRequest(resource='subnet', msg=msg)
        params = {
            'parent_id': subnet_l2dom['nuage_subnet_id'],
            'type': subnet_l2dom['nuage_l2dom_tmplt_id']
        }
        with context.session.begin(subtransactions=True):
            original_subnet = self.get_subnet(context, id)
            updated_subnet = super(NuagePlugin, self).update_subnet(
                context, id, subnet)

            curr_enable_dhcp = original_subnet.get('enable_dhcp')
            updated_enable_dhcp = updated_subnet.get('enable_dhcp')

            if not curr_enable_dhcp and updated_enable_dhcp:
                last_address = updated_subnet['allocation_pools'][-1]['end']
                gw_port = self._create_port_gateway(context,
                                                    updated_subnet,
                                                    last_address)
                params['net'] = netaddr.IPNetwork(original_subnet['cidr'])
                params['dhcp_ip'] = gw_port['fixed_ips'][0]['ip_address']
            elif curr_enable_dhcp and not updated_enable_dhcp:
                params['dhcp_ip'] = None
                filters = {
                    'fixed_ips': {'subnet_id': [id]},
                    'device_owner': [constants.DEVICE_OWNER_DHCP_NUAGE]
                }
                gw_ports = self.get_ports(context, filters=filters)
                self._delete_port_gateway(context, gw_ports)

            self.nuageclient.update_subnet(subn, params)

            return updated_subnet

    @nuage_utils.handle_nuage_api_error
    @log.log
    def delete_subnet(self, context, id):
        subnet = self.get_subnet(context, id)

        filters = {
            'fixed_ips': {'subnet_id': [id]},
            'device_owner': [constants.DEVICE_OWNER_DHCP_NUAGE]
        }
        ports = self.get_ports(context, filters)
        for port in ports:
            if port['device_owner'] != constants.DEVICE_OWNER_DHCP_NUAGE:
                raise n_exc.SubnetInUse(subnet_id=id)
        self._delete_port_gateway(context, ports)

        if self._network_is_external(context, subnet['network_id']):
            LOG.debug("Network %s is external, so deleting the sharedresource",
                      subnet['network_id'])
            super(NuagePlugin, self).delete_subnet(context, id)
            return self._delete_nuage_sharedresource(id)

        subnet_l2dom = nuagedb.get_subnet_l2dom_by_id(context.session, id)
        if subnet_l2dom:
            LOG.debug("Found l2domain mapping for subnet %s", id)
            try:
                self.nuageclient.delete_subnet(id)
            except Exception:
                msg = (_('Unable to complete operation on subnet %s.'
                         'One or more ports have an IP allocation '
                         'from this subnet.') % id)
                raise n_exc.BadRequest(resource='subnet', msg=msg)
        super(NuagePlugin, self).delete_subnet(context, id)

        if subnet_l2dom:
            if subnet_l2dom['nuage_managed_subnet']:
                self.nuageclient.detach_nuage_group_to_nuagenet(
                    context.tenant, subnet_l2dom['net_partition_id'],
                    subnet_l2dom['nuage_subnet_id'], subnet['shared'])

            if not self._check_router_subnet_for_tenant(
                context, subnet['tenant_id']):
                LOG.debug("No router/subnet found for tenant %s", subnet[
                    'tenant_id'])
                self.nuageclient.delete_user(subnet_l2dom['nuage_user_id'])
                self.nuageclient.delete_group(subnet_l2dom['nuage_group_id'])

    @nuage_utils.handle_nuage_api_error
    @log.log
    def add_router_interface(self, context, router_id, interface_info):
        session = context.session
        with session.begin(subtransactions=True):
            rtr_if_info = super(NuagePlugin,
                                self).add_router_interface(context,
                                                           router_id,
                                                           interface_info)
            subnet_id = rtr_if_info['subnet_id']
            subn = self.get_subnet(context, subnet_id)
            ent_rtr_mapping = nuagedb.get_ent_rtr_mapping_by_rtrid(session,
                                                                   router_id)
            nuage_zone = self.nuageclient.get_zone_by_routerid(router_id,
                                                               subn['shared'])
            if not nuage_zone or not ent_rtr_mapping:
                super(NuagePlugin,
                      self).remove_router_interface(context,
                                                    router_id,
                                                    interface_info)
                msg = (_("Router %s does not hold default zone OR "
                         "domain in VSD. Router-IF add failed")
                       % router_id)
                raise n_exc.BadRequest(resource='router', msg=msg)

            subnet_l2dom = nuagedb.get_subnet_l2dom_by_id(session,
                                                          subnet_id)
            if not subnet_l2dom:
                super(NuagePlugin,
                      self).remove_router_interface(context,
                                                    router_id,
                                                    interface_info)
                msg = (_("Subnet %s does not hold Nuage VSD reference. "
                         "Router-IF add failed") % subnet_id)
                raise n_exc.BadRequest(resource='subnet', msg=msg)

            if subnet_l2dom['nuage_managed_subnet']:
                super(NuagePlugin,
                      self).remove_router_interface(context,
                                                    router_id,
                                                    interface_info)
                msg = ("Subnet %s is a VSD-Managed subnet."
                       "Router-IF add failed" % rtr_if_info['subnet_id'])
                raise n_exc.BadRequest(resource='subnet', msg=msg)

            if (subnet_l2dom['net_partition_id'] !=
                ent_rtr_mapping['net_partition_id']):
                super(NuagePlugin,
                      self).remove_router_interface(context,
                                                    router_id,
                                                    interface_info)
                msg = (_("Subnet %(subnet)s and Router %(router)s belong to "
                         "different net_partition Router-IF add "
                         "not permitted") % {'subnet': subnet_id,
                                             'router': router_id})
                raise n_exc.BadRequest(resource='subnet', msg=msg)
            nuage_subnet_id = subnet_l2dom['nuage_subnet_id']
            if self.nuageclient.vms_on_l2domain(nuage_subnet_id):
                super(NuagePlugin,
                      self).remove_router_interface(context,
                                                    router_id,
                                                    interface_info)
                msg = (_("Subnet %s has one or more active VMs "
                       "Router-IF add not permitted") % subnet_id)
                raise n_exc.BadRequest(resource='subnet', msg=msg)

            nuage_rtr_id = ent_rtr_mapping['nuage_router_id']
            if self.nuageclient.validate_create_domain_subnet(
                subn, nuage_subnet_id, nuage_rtr_id):
                self.nuageclient.delete_subnet(subnet_id)
                LOG.debug("Deleted l2 domain %s", nuage_subnet_id)

            filters = {
                'fixed_ips': {'subnet_id': [subnet_id]},
                'device_owner': [constants.DEVICE_OWNER_DHCP_NUAGE]
            }
            gw_ports = self.get_ports(context, filters=filters)
            self._delete_port_gateway(context, gw_ports)
            net = netaddr.IPNetwork(subn['cidr'])
            pnet_binding = nuagedb.get_network_binding(context.session,
                                                       subn['network_id'])
            params = {
                'net': net,
                'zone_id': nuage_zone['nuage_zone_id'],
                'neutron_subnet_id': subnet_id,
                'pnet_binding': pnet_binding
            }
            if not attributes.is_attr_set(subn['gateway_ip']):
                subn['gateway_ip'] = str(netaddr.IPAddress(net.first + 1))

            try:
                nuage_subnet = self.nuageclient.create_domain_subnet(subn,
                                                                   params)
            except Exception:
                with excutils.save_and_reraise_exception():
                    super(NuagePlugin,
                          self).remove_router_interface(context,
                                                        router_id,
                                                        interface_info)

            if nuage_subnet:
                LOG.debug("Created nuage domain %s",
                          nuage_subnet['nuage_subnetid'])
                ns_dict = {}
                ns_dict['nuage_subnet_id'] = nuage_subnet['nuage_subnetid']
                ns_dict['nuage_l2dom_tmplt_id'] = None
                nuagedb.update_subnetl2dom_mapping(subnet_l2dom,
                                                   ns_dict)

        return rtr_if_info

    @nuage_utils.handle_nuage_api_error
    @log.log
    def remove_router_interface(self, context, router_id, interface_info):
        if 'subnet_id' in interface_info:
            subnet_id = interface_info['subnet_id']
            subnet = self.get_subnet(context, subnet_id)
            found = False
            try:
                filters = {'device_id': [router_id],
                           'device_owner':
                           [os_constants.DEVICE_OWNER_ROUTER_INTF],
                           'network_id': [subnet['network_id']]}
                ports = self.get_ports(context, filters)

                for p in ports:
                    if p['fixed_ips'][0]['subnet_id'] == subnet_id:
                        found = True
                        break
            except exc.NoResultFound:
                msg = (_("No router interface found for Router %s. "
                         "Router-IF delete failed") % router_id)
                raise n_exc.BadRequest(resource='router', msg=msg)

            if not found:
                msg = (_("No router interface found for Router %s. "
                         "Router-IF delete failed") % router_id)
                raise n_exc.BadRequest(resource='router', msg=msg)
        elif 'port_id' in interface_info:
            port_db = self._get_port(context, interface_info['port_id'])
            if not port_db:
                msg = (_("No router interface found for Router %s. "
                         "Router-IF delete failed") % router_id)
                raise n_exc.BadRequest(resource='router', msg=msg)
            subnet_id = port_db['fixed_ips'][0]['subnet_id']

        session = context.session
        with session.begin(subtransactions=True):
            subnet_l2dom = nuagedb.get_subnet_l2dom_by_id(session,
                                                          subnet_id)
            if not subnet_l2dom:
                return super(NuagePlugin,
                             self).remove_router_interface(context,
                                                           router_id,
                                                           interface_info)
            nuage_subn_id = subnet_l2dom['nuage_subnet_id']
            if self.nuageclient.vms_on_subnet(nuage_subn_id):
                msg = (_("Subnet %s has one or more active VMs "
                         "Router-IF delete not permitted") % subnet_id)
                raise n_exc.BadRequest(resource='subnet', msg=msg)

            neutron_subnet = self.get_subnet(context, subnet_id)
            ent_rtr_mapping = nuagedb.get_ent_rtr_mapping_by_rtrid(
                context.session,
                router_id)
            if not ent_rtr_mapping:
                msg = (_("Router %s does not hold net_partition "
                         "assoc on Nuage VSD. Router-IF delete failed")
                       % router_id)
                raise n_exc.BadRequest(resource='router', msg=msg)

            last_address = neutron_subnet['allocation_pools'][-1]['end']
            gw_port = self._create_port_gateway(context, neutron_subnet,
                                                last_address)
            net = netaddr.IPNetwork(neutron_subnet['cidr'])
            netpart_id = ent_rtr_mapping['net_partition_id']
            pnet_binding = nuagedb.get_network_binding(
                context.session, neutron_subnet['network_id'])

            neutron_net = self.get_network(context,
                                           neutron_subnet['network_id'])

            params = {
                'tenant_id': neutron_subnet['tenant_id'],
                'net': net,
                'netpart_id': netpart_id,
                'nuage_subn_id': nuage_subn_id,
                'neutron_subnet': neutron_subnet,
                'pnet_binding': pnet_binding,
                'dhcp_ip': gw_port['fixed_ips'][0]['ip_address'],
                'neutron_router_id': router_id,
                'shared': neutron_net['shared']
            }
            nuage_subnet = self.nuageclient.remove_router_interface(params)
            LOG.debug("Deleted nuage domain subnet %s", nuage_subn_id)
            info = super(NuagePlugin,
                         self).remove_router_interface(context, router_id,
                                                       interface_info)

            if nuage_subnet:
                tmplt_id = str(nuage_subnet['nuage_l2template_id'])
                ns_dict = {}
                ns_dict['nuage_subnet_id'] = nuage_subnet['nuage_l2domain_id']
                ns_dict['nuage_l2dom_tmplt_id'] = tmplt_id
                nuagedb.update_subnetl2dom_mapping(subnet_l2dom,
                                                   ns_dict)
        return info

    @log.log
    def _get_net_partition_for_router(self, context, rtr):
        ent = rtr.get('net_partition', None)
        if not ent:
            def_net_part = cfg.CONF.RESTPROXY.default_net_partition_name
            net_partition = nuagedb.get_net_partition_by_name(context.session,
                                                              def_net_part)
        else:
            net_partition = self._resource_finder(context, 'router',
                                                  'net_partition', rtr)
        if not net_partition:
            msg = _("Either net_partition is not provided with router OR "
                    "default net_partition is not created at the start")
            raise n_exc.BadRequest(resource='router', msg=msg)
        return net_partition

    @nuage_utils.handle_nuage_api_error
    @log.log
    def get_router(self, context, id, fields=None):
        router = super(NuagePlugin, self).get_router(context, id, fields)
        nuage_router = self.nuageclient.get_router_by_external(id)
        if nuage_router:
            if not fields or 'tunnel_type' in fields:
                router['tunnel_type'] = nuage_router['tunnelType']
            if not fields or 'rd' in fields:
                router['rd'] = nuage_router['routeDistinguisher']
            if not fields or 'rt' in fields:
                router['rt'] = nuage_router['routeTarget']
        return router

    @nuage_utils.handle_nuage_api_error
    @log.log
    def create_router(self, context, router):
        req_router = copy.deepcopy(router['router'])
        net_partition = self._get_net_partition_for_router(context,
                                                           router['router'])
        if (cfg.CONF.RESTPROXY.nuage_pat == 'notavailable' and
            req_router.get('external_gateway_info')):
            msg = _("nuage_pat config is set to 'notavailable'. "
                    "Can't set ext-gw-info")
            raise nuage_exc.OperationNotSupported(resource='router', msg=msg)

        neutron_router = super(NuagePlugin, self).create_router(context,
                                                                router)
        params = {
            'net_partition': net_partition,
            'tenant_id': neutron_router['tenant_id'],
            'nuage_pat': cfg.CONF.RESTPROXY.nuage_pat
        }
        try:
            nuage_router = self.nuageclient.create_router(neutron_router,
                                                          req_router,
                                                          params)
        except Exception:
            with excutils.save_and_reraise_exception():
                super(NuagePlugin, self).delete_router(context,
                                                       neutron_router['id'])

        if nuage_router:
            LOG.debug("Created nuage domain %s", nuage_router[
                'nuage_domain_id'])
            with context.session.begin(subtransactions=True):
                nuagedb.add_entrouter_mapping(context.session,
                                              net_partition['id'],
                                              neutron_router['id'],
                                              nuage_router['nuage_domain_id'],
                                              nuage_router['rt'],
                                              nuage_router['rd'])
            neutron_router['tunnel_type'] = nuage_router['tunnel_type']
            neutron_router['rd'] = nuage_router['rd']
            neutron_router['rt'] = nuage_router['rt']

        return neutron_router

    @log.log
    def _validate_nuage_staticroutes(self, old_routes, added, removed):
        cidrs = []
        for old in old_routes:
            if old not in removed:
                ip = netaddr.IPNetwork(old['destination'])
                cidrs.append(ip)
        for route in added:
            ip = netaddr.IPNetwork(route['destination'])
            matching = netaddr.all_matching_cidrs(ip.ip, cidrs)
            if matching:
                msg = _('for same subnet, multiple static routes not allowed')
                raise n_exc.BadRequest(resource='router', msg=msg)
            cidrs.append(ip)

    @nuage_utils.handle_nuage_api_error
    @log.log
    def update_router(self, context, id, router):
        r = router['router']
        if (cfg.CONF.RESTPROXY.nuage_pat == 'notavailable' and
            r.get('external_gateway_info')):
            msg = _("nuage_pat config is set to 'notavailable'. "
                    "Can't update ext-gw-info")
            raise nuage_exc.OperationNotSupported(resource='router', msg=msg)

        with context.session.begin(subtransactions=True):
            if 'routes' in r:
                old_routes = self._get_extra_routes_by_router_id(context,
                                                                 id)
                added, removed = utils.diff_list_of_dict(old_routes,
                                                         r['routes'])
                self._validate_nuage_staticroutes(old_routes, added, removed)

                ent_rtr_mapping = nuagedb.get_ent_rtr_mapping_by_rtrid(
                    context.session, id)
                if not ent_rtr_mapping:
                    msg = (_("Router %s does not hold net-partition "
                             "assoc on VSD. extra-route failed") % id)
                    raise n_exc.BadRequest(resource='router', msg=msg)
                # Let it do internal checks first and verify it.
                router_updated = super(NuagePlugin,
                                       self).update_router(context,
                                                           id,
                                                           router)
                for route in removed:
                    destaddr = route['destination']
                    cidr = destaddr.split('/')
                    params = {
                        "address": cidr[0],
                        "nexthop": route['nexthop'],
                        "nuage_domain_id": ent_rtr_mapping['nuage_router_id']
                    }
                    self.nuageclient.delete_nuage_staticroute(params)

                for route in added:
                    params = {
                        'parent_id': ent_rtr_mapping['nuage_router_id'],
                        'net': netaddr.IPNetwork(route['destination']),
                        'nexthop': route['nexthop']
                    }
                    self.nuageclient.create_nuage_staticroute(
                        params)
                return router_updated
            elif 'external_gateway_info' in r:
                curr_router = self.get_router(context, id)
                router_updated = super(NuagePlugin, self).update_router(
                    context, id, router)
                curr_ext_gw_info = curr_router['external_gateway_info']
                new_ext_gw_info = router_updated['external_gateway_info']
                send_update = False
                if curr_ext_gw_info and not new_ext_gw_info:
                    if curr_ext_gw_info['enable_snat']:
                        send_update = True
                elif not curr_ext_gw_info and new_ext_gw_info:
                    if new_ext_gw_info['enable_snat']:
                        send_update = True
                elif (curr_ext_gw_info and
                      new_ext_gw_info and
                      curr_ext_gw_info['enable_snat'] !=
                      new_ext_gw_info['enable_snat']):
                    send_update = True
                if send_update:
                    self.nuageclient.update_router_gw(
                        router_updated, params={
                        'nuage_pat': cfg.CONF.RESTPROXY.nuage_pat})
            else:
                neutron_router = None
                if ('rd' in r.keys() and r['rd'] or
                        'rt' in r.keys() and r['rt']):
                    neutron_router = self.get_router(context, id)
                    net_partition = self._get_net_partition_for_router(
                        context, router)

                    params = {
                        'net_partition': net_partition,
                        'tenant_id': neutron_router['tenant_id']
                    }

                    ent_rtr_mapping = nuagedb.get_ent_rtr_mapping_by_rtrid(
                        context.session, id)
                    if (ent_rtr_mapping and
                            (neutron_router['rd'] != r['rd'] or
                             neutron_router['rt'] != r['rt'])):
                        nuage_domain_id = ent_rtr_mapping['nuage_router_id']
                        self.nuageclient.update_router_rt_rd(neutron_router,
                                                             router['router'],
                                                             nuage_domain_id,
                                                             params)
                        ns_dict = {}
                        ns_dict['nuage_rtr_rt'] = r['rt']
                        ns_dict['nuage_rtr_rd'] = r['rd']
                        nuagedb.update_entrouter_mapping(ent_rtr_mapping,
                                                         ns_dict)
                if r.get('tunnel_type'):
                    if not neutron_router:
                        neutron_router = self.get_router(context, id)
                    if neutron_router['tunnel_type'] != r['tunnel_type']:
                        net_partition = self._get_net_partition_for_router(
                            context, router)
                        ent_rtr_mapping = nuagedb.get_ent_rtr_mapping_by_rtrid(
                            context.session, id)

                        nuage_domain_id = ent_rtr_mapping['nuage_router_id']
                        self.nuageclient.update_router_tunnel_type(
                            neutron_router, router['router'], net_partition,
                            nuage_domain_id)

                router_updated = super(NuagePlugin, self).update_router(
                    context, id, router)
        return router_updated

    @nuage_utils.handle_nuage_api_error
    @log.log
    def delete_router(self, context, id):
        neutron_router = self.get_router(context, id)
        session = context.session
        ent_rtr_mapping = nuagedb.get_ent_rtr_mapping_by_rtrid(session,
                                                               id)
        if ent_rtr_mapping:
            LOG.debug("Enterprise to router mapping found for router %s", id)
            filters = {
                'device_id': [id],
                'device_owner': [os_constants.DEVICE_OWNER_ROUTER_INTF]
            }
            ports = self.get_ports(context, filters)
            if ports:
                raise l3.RouterInUse(router_id=id)
            nuage_domain_id = ent_rtr_mapping['nuage_router_id']
            self.nuageclient.delete_router(nuage_domain_id)

        super(NuagePlugin, self).delete_router(context, id)

        if not self._check_router_subnet_for_tenant(
                context, neutron_router['tenant_id']):
            LOG.debug("No router/subnet found for tenant %s",
                      neutron_router['tenant_id'])
            user_id, group_id = self.nuageclient.get_usergroup(
                neutron_router['tenant_id'],
                ent_rtr_mapping['net_partition_id'])
            self.nuageclient.delete_user(user_id)
            self.nuageclient.delete_group(group_id)

    @log.log
    def _make_net_partition_dict(self, net_partition,
                                 context=None, fields=None):
        res = {
            'id': net_partition['id'],
            'name': net_partition['name'],
            'l3dom_tmplt_id': net_partition['l3dom_tmplt_id'],
            'l2dom_tmplt_id': net_partition['l2dom_tmplt_id'],
            'isolated_zone': net_partition['isolated_zone'],
            'shared_zone': net_partition['shared_zone']
        }
        if context:
            res['tenant_id'] = context.tenant_id
        return self._fields(res, fields)

    @log.log
    def _create_net_partition(self, session, net_part_name):
        params = {
            "name": net_part_name,
            "fp_quota": str(cfg.CONF.RESTPROXY.default_floatingip_quota)
        }
        nuage_net_partition = self.nuageclient.create_net_partition(params)
        net_partitioninst = None
        if nuage_net_partition:
            with session.begin(subtransactions=True):
                net_partitioninst = NuagePlugin._add_net_partition(
                    session,
                    nuage_net_partition,
                    net_part_name)
        if not net_partitioninst:
            return {}
        return self._make_net_partition_dict(net_partitioninst)

    @log.log
    def _validate_create_net_partition(self,
                                       net_part_name,
                                       session=db.get_session()):
        nuage_netpart = self.nuageclient.get_netpartition_data(
            net_part_name)
        netpart_db = nuagedb.get_net_partition_by_name(session, net_part_name)

        if nuage_netpart:
            with session.begin(subtransactions=True):
                if netpart_db:
                    # Net-partition exists in neutron and vsd
                    def_netpart = (
                        cfg.CONF.RESTPROXY.default_net_partition_name)
                    if def_netpart == net_part_name:
                        if nuage_netpart['np_id'] != netpart_db['id']:
                            msg = ("Default net-partition %s exists in "
                                   "Neutron and VSD, but the id is different"
                                   % net_part_name)
                            raise n_exc.BadRequest(resource='net_partition',
                                                   msg=msg)
                        LOG.info("Default net-partition %s already exists,"
                                 " so will just use it" % net_part_name)
                        return self._make_net_partition_dict(netpart_db)
                    else:
                        if nuage_netpart['np_id'] != netpart_db['id']:
                            msg = (('Net-partition %s already exists in '
                                    'Neutron and VSD, but the id is '
                                    'different') % net_part_name)
                        else:
                            msg = (('Net-partition %s already exists in '
                                    'Neutron and VSD with same id') %
                                   net_part_name)

                        raise n_exc.BadRequest(resource='net_partition',
                                               msg=msg)

                # Net-partition exists in vsd and not in neutron
                netpart_db = NuagePlugin._add_net_partition(session,
                                                            nuage_netpart,
                                                            net_part_name)
                return self._make_net_partition_dict(netpart_db)
        else:

            if netpart_db:
                # Net-partition exists in neutron and not VSD
                LOG.info("Existing net-partition %s will be deleted and "
                         "re-created in db", net_part_name)
                nuagedb.delete_net_partition(session, netpart_db)

            # Net-partition does not exist in neutron and VSD
            return self._create_net_partition(session, net_part_name)

    @staticmethod
    @log.log
    def _add_net_partition(session, netpart, netpart_name):
        l3dom_id = netpart['l3dom_tid']
        l3isolated = constants.DEF_NUAGE_ZONE_PREFIX + '-' + l3dom_id
        l3shared = constants.DEF_NUAGE_ZONE_PREFIX + '-pub-' + l3dom_id
        return nuagedb.add_net_partition(session,
                                         netpart['np_id'],
                                         l3dom_id,
                                         netpart['l2dom_tid'],
                                         netpart_name,
                                         l3isolated,
                                         l3shared)

    @log.log
    def _link_default_netpartition(self, netpart_name,
                                   l2template, l3template,
                                   l3isolated, l3shared):
        params = {
            'name': netpart_name,
            'l3template': l3template,
            'l2template': l2template
        }
        (np_id, l3dom_tid,
         l2dom_tid) = self.nuageclient.link_default_netpartition(params)
        #verify that the provided zones have been created already
        shared_match, isolated_match = self.nuageclient.validate_zone_create(
            l3dom_tid, l3isolated, l3shared)
        if not shared_match or not isolated_match:
            msg = ('Default zone names must be provided for '
                   'default net-partiton')
            raise n_exc.BadRequest(resource='net_partition', msg=msg)

        # basic verifications passed. add default netpartition to the DB
        session = db.get_session()
        netpartition = nuagedb.get_net_partition_by_name(session,
                                                         netpart_name)

        with session.begin():
            if netpartition:
                nuagedb.delete_net_partition(session, netpartition)
            net_partitioninst = nuagedb.add_net_partition(session,
                                                          np_id,
                                                          l3dom_tid,
                                                          l2dom_tid,
                                                          netpart_name,
                                                          l3isolated,
                                                          l3shared)
        return net_partitioninst

    @log.log
    def _prepare_default_netpartition(self):
        netpart_name = cfg.CONF.RESTPROXY.default_net_partition_name
        l3template = cfg.CONF.RESTPROXY.default_l3domain_template
        l2template = cfg.CONF.RESTPROXY.default_l2domain_template
        l3isolated = cfg.CONF.RESTPROXY.default_isolated_zone
        l3shared = cfg.CONF.RESTPROXY.default_shared_zone

        # if templates are not provided, create default templates
        if l2template or l3template or l3isolated or l3shared:
            if (not l2template or not l3template or not l3isolated or
                    not l3shared):
                msg = 'Configuration of default net-partition not complete'
                raise n_exc.BadRequest(resource='net_partition',
                                       msg=msg)
        else:
            return self._validate_create_net_partition(netpart_name)

        '''NetPartition and templates already created. Just sync the
        neutron DB. They must all be in VSD. If not, its an error
        '''
        return self._link_default_netpartition(netpart_name,
                                               l2template,
                                               l3template,
                                               l3isolated,
                                               l3shared)

    @nuage_utils.handle_nuage_api_error
    @log.log
    def create_net_partition(self, context, net_partition):
        ent = net_partition['net_partition']
        return self._validate_create_net_partition(ent["name"], context.session)

    @nuage_utils.handle_nuage_api_error
    @log.log
    def delete_net_partition(self, context, id):
        ent_rtr_mapping = nuagedb.get_ent_rtr_mapping_by_entid(
            context.session, id)
        if ent_rtr_mapping:
            msg = (_("One or more router still attached to "
                     "net_partition %s.") % id)
            raise n_exc.BadRequest(resource='net_partition', msg=msg)
        net_partition = nuagedb.get_net_partition_by_id(context.session, id)
        if not net_partition:
            raise nuage_exc.NuageNotFound(resource='net_partition',
                                          resource_id=id)
        l3dom_tmplt_id = net_partition['l3dom_tmplt_id']
        l2dom_tmplt_id = net_partition['l2dom_tmplt_id']
        self.nuageclient.delete_net_partition(net_partition['id'],
                                              l3dom_id=l3dom_tmplt_id,
                                              l2dom_id=l2dom_tmplt_id)
        with context.session.begin(subtransactions=True):
            nuagedb.delete_net_partition(context.session,
                                         net_partition)

    @log.log
    def get_net_partition(self, context, id, fields=None):
        net_partition = nuagedb.get_net_partition_by_id(context.session,
                                                        id)
        if not net_partition:
            raise nuage_exc.NuageNotFound(resource='net_partition',
                                          resource_id=id)
        return self._make_net_partition_dict(net_partition, context=context)

    @log.log
    def get_net_partitions(self, context, filters=None, fields=None):
        net_partitions = nuagedb.get_net_partitions(context.session,
                                                    filters=filters,
                                                    fields=fields)
        return [self._make_net_partition_dict(net_partition, context, fields)
                for net_partition in net_partitions]

    @log.log
    def _check_floatingip_update(self, context, port,
                                 vport_type=constants.VM_VPORT,
                                 vport_id=None):
        filter = {'fixed_port_id': [port['id']]}
        local_fip = self.get_floatingips(context,
                                         filters=filter)
        if local_fip:
            fip = local_fip[0]
            self._create_update_floatingip(context,
                                           fip, port['id'],
                                           vport_type=vport_type,
                                           vport_id=vport_id)

    @log.log
    def _create_update_floatingip(self, context,
                                  neutron_fip, port_id,
                                  last_known_router_id=None,
                                  vport_type=constants.VM_VPORT,
                                  vport_id=None):
        if last_known_router_id:
            rtr_id = last_known_router_id
        else:
            rtr_id = neutron_fip['router_id']
        net_id = neutron_fip['floating_network_id']
        subn = nuagedb.get_ipalloc_for_fip(context.session,
                                           net_id,
                                           neutron_fip['floating_ip_address'])

        fip_pool = self.nuageclient.get_nuage_fip_pool_by_id(subn['subnet_id'])
        if not fip_pool:
            msg = _('sharedresource %s not found on VSD') % subn['subnet_id']
            raise n_exc.BadRequest(resource='floatingip',
                                   msg=msg)

        ent_rtr_mapping = nuagedb.get_ent_rtr_mapping_by_rtrid(context.session,
                                                               rtr_id)
        if not ent_rtr_mapping:
            msg = _('router %s is not associated with '
                    'any net-partition') % rtr_id
            raise n_exc.BadRequest(resource='floatingip',
                                   msg=msg)

        params = {
            'router_id': ent_rtr_mapping['nuage_router_id'],
            'fip_id': neutron_fip['id'],
            'neutron_fip': neutron_fip
        }

        fip = self.nuageclient.get_nuage_fip_by_id(params)
        if not fip:
            LOG.debug("Floating ip not found in VSD for fip %s",
                      neutron_fip['id'])
            params = {
                'nuage_rtr_id': ent_rtr_mapping['nuage_router_id'],
                'nuage_fippool_id': fip_pool['nuage_fip_pool_id'],
                'neutron_fip_ip': neutron_fip['floating_ip_address'],
                'neutron_fip_id': neutron_fip['id']
            }
            nuage_fip_id = self.nuageclient.create_nuage_floatingip(params)
        else:
            nuage_fip_id = fip['nuage_fip_id']

        # Update VM if required
        params = {
            'neutron_port_id': port_id,
            'nuage_fip_id': nuage_fip_id,
            'nuage_rtr_id': ent_rtr_mapping['nuage_router_id'],
            'nuage_vport_type': vport_type,
            'nuage_vport_id': vport_id
        }
        nuage_port = self.nuageclient.get_nuage_port_by_id(params)
        if nuage_port:
            if (nuage_port['nuage_domain_id']) != (
                    ent_rtr_mapping['nuage_router_id']):
                msg = _('Floating IP can not be associated to VM in '
                        'different router context')
                raise nuage_exc.OperationNotSupported(msg=msg)

            params = {
                'nuage_vport_id': nuage_port['nuage_vport_id'],
                'nuage_fip_id': nuage_fip_id
            }
            self.nuageclient.update_nuage_vm_vport(params)
            self.fip_rate_log.info(
                _('FIP %s (owned by tenant %s) associated to port %s'
                  % (neutron_fip['id'], neutron_fip['tenant_id'], port_id)))

        # Add QOS to port for rate limiting
        if neutron_fip.get('nuage_fip_rate', None):
            if not nuage_port:
                msg = _('Rate limiting requires the floating ip to be '
                        'associated to a port.')
                raise nuage_exc.NuageBadRequest(msg=msg)
            self.nuageclient.create_update_rate_limiting(
                neutron_fip['nuage_fip_rate'], nuage_port['nuage_vport_id'],
                neutron_fip['id'])
            self.fip_rate_log.info(
                _('FIP %s (owned by tenant %s) rate limit updated to %s Mb/s' %
                  (neutron_fip['id'], neutron_fip['tenant_id'],
                   (neutron_fip['nuage_fip_rate']
                    if neutron_fip['nuage_fip_rate']
                    else "unlimited"))))

    @nuage_utils.handle_nuage_api_error
    @log.log
    def get_floatingip(self, context, id, fields=None):
        fip = super(NuagePlugin, self).get_floatingip(context, id)

        if (not fields or 'nuage_fip_rate' in fields
                and 'port_id' in fip):
            params = {
                'neutron_port_id': fip['port_id'],
            }
            nuage_port = self.nuageclient.get_nuage_port_by_id(params)
            if nuage_port:
                rate_limit = self.nuageclient.get_rate_limit(
                    nuage_port['nuage_vport_id'], fip['id'])
                fip['nuage_fip_rate'] = rate_limit

        return self._fields(fip, fields)

    @nuage_utils.handle_nuage_api_error
    @log.log
    def create_floatingip(self, context, floatingip):
        fip = floatingip['floatingip']
        with context.session.begin(subtransactions=True):
            neutron_fip = super(NuagePlugin, self).create_floatingip(
                context, floatingip)
            if fip.get('nuage_fip_rate'):
                if not fip.get('port_id'):
                    msg = _('Rate limiting requires the floating ip to be '
                            'associated to a port.')
                    raise nuage_exc.NuageBadRequest(msg=msg)
                neutron_fip['nuage_fip_rate'] = fip['nuage_fip_rate']
            else:
                neutron_fip['nuage_fip_rate'] = self.def_fip_rate

            if not neutron_fip['router_id']:
                return neutron_fip

            try:
                self._create_update_floatingip(context, neutron_fip,
                                               fip['port_id'])
            except (nuage_exc.OperationNotSupported, n_exc.BadRequest):
                with excutils.save_and_reraise_exception():
                    super(NuagePlugin, self).delete_floatingip(
                        context, neutron_fip['id'])
            return neutron_fip

    @nuage_utils.handle_nuage_api_error
    @log.log
    def disassociate_floatingips(self, context, port_id, do_notify=True):
        fips = self.get_floatingips(context, filters={'port_id': [port_id]})

        router_ids = super(NuagePlugin, self).disassociate_floatingips(
            context, port_id, do_notify=do_notify)

        params = {
            'neutron_port_id': port_id,
        }
        nuage_port = self.nuageclient.get_nuage_port_by_id(params)
        if nuage_port:
            for fip in fips:
                self.nuageclient.delete_rate_limiting(
                    nuage_port['nuage_vport_id'], fip['id'])
                self.fip_rate_log.info('FIP %s (owned by tenant %s) '
                                       'disassociated from port %s'
                                       % (fip['id'], fip['tenant_id'], port_id))
            params = {
                'nuage_vport_id': nuage_port['nuage_vport_id'],
                'nuage_fip_id': None
            }
            self.nuageclient.update_nuage_vm_vport(params)
            LOG.debug("Disassociated floating ip from VM attached at port %s",
                      port_id)

        return router_ids

    @nuage_utils.handle_nuage_api_error
    @log.log
    def update_floatingip(self, context, id, floatingip):
        fip = floatingip['floatingip']
        orig_fip = self._get_floatingip(context, id)
        port_id = orig_fip['fixed_port_id']
        last_known_router_id = orig_fip['last_known_router_id']
        router_ids = []
        with context.session.begin(subtransactions=True):
            if 'port_id' in fip:
                neutron_fip = super(NuagePlugin, self).update_floatingip(
                    context, id, floatingip)
            if fip.get('port_id'):
                if not neutron_fip['router_id']:
                    ret_msg = 'floating-ip is not associated yet'
                    raise n_exc.BadRequest(resource='floatingip',
                                           msg=ret_msg)
                if not fip.get('nuage_fip_rate'):
                    neutron_fip['nuage_fip_rate'] = self.def_fip_rate
                else:
                    neutron_fip['nuage_fip_rate'] = fip['nuage_fip_rate']

                try:
                    self._create_update_floatingip(context,
                                                   neutron_fip,
                                                   fip['port_id'],
                                                   last_known_router_id)
                except nuage_exc.OperationNotSupported:
                    with excutils.save_and_reraise_exception():
                        router_ids = super(
                            NuagePlugin, self).disassociate_floatingips(
                                context, fip['port_id'], do_notify=False)
                except n_exc.BadRequest:
                    with excutils.save_and_reraise_exception():
                        super(NuagePlugin, self).delete_floatingip(
                            context, id)
            elif 'port_id' in fip:
                # This happens when {'port_id': null} is in request.
                # Disassociate
                params = {
                    'neutron_port_id': port_id,
                }
                nuage_port = self.nuageclient.get_nuage_port_by_id(params)
                if nuage_port:
                    params = {
                        'nuage_vport_id': nuage_port['nuage_vport_id'],
                        'nuage_fip_id': None
                    }
                    self.nuageclient.update_nuage_vm_vport(params)
                    self.nuageclient.delete_rate_limiting(
                        nuage_port['nuage_vport_id'], fip['id'])
                    self.fip_rate_log.info('FIP %s (owned by tenant %s) '
                                           'disassociated from port %s'
                                           % (id, fip['tenant_id'], port_id))

        #purely rate limit update. Use existing port data.
        if 'port_id' not in fip and 'nuage_fip_rate' in fip:
            if not port_id:
                msg = _('nuage-fip-rate can only be applied to floatingips '
                        'associated to a port')
                raise n_exc.BadRequest(resource='floatingip', msg=msg)
            # Add QOS to port for rate limiting
            params = {
                'neutron_port_id': port_id,
            }
            nuage_port = self.nuageclient.get_nuage_port_by_id(params)

            if fip['nuage_fip_rate'] is None:
                orig_fip['nuage_fip_rate'] = self.def_fip_rate
            else:
                orig_fip['nuage_fip_rate'] = fip['nuage_fip_rate']

            self.nuageclient.create_update_rate_limiting(
                orig_fip['nuage_fip_rate'], nuage_port['nuage_vport_id'],
                orig_fip['id'])
            self.fip_rate_log.info(
                _('FIP %s (owned by tenant %s) rate limit updated to %s Mb/s'
                  % (orig_fip['id'], orig_fip['tenant_id'],
                     (orig_fip['nuage_fip_rate']
                      if (orig_fip['nuage_fip_rate']
                          and orig_fip['nuage_fip_rate'] != -1)
                      else "unlimited"))))
            neutron_fip = self._make_floatingip_dict(orig_fip)
            neutron_fip['nuage_fip_rate'] = orig_fip['nuage_fip_rate']

        # now that we've left db transaction, we are safe to notify
        self.notify_routers_updated(context, router_ids)

        return neutron_fip

    @nuage_utils.handle_nuage_api_error
    @log.log
    def delete_floatingip(self, context, fip_id):
        fip = self._get_floatingip(context, fip_id)
        port_id = fip['fixed_port_id']
        with context.session.begin(subtransactions=True):
            if port_id:
                params = {
                    'neutron_port_id': port_id,
                }
                nuage_port = self.nuageclient.get_nuage_port_by_id(params)
                if (nuage_port and
                    nuage_port['nuage_vport_id'] is not None):
                    params = {
                        'nuage_vport_id': nuage_port['nuage_vport_id'],
                        'nuage_fip_id': None
                    }
                    self.nuageclient.update_nuage_vm_vport(params)
                    LOG.debug("Floating-ip %(fip)s is disassociated from "
                              "vport %(vport)s",
                              {'fip': fip_id,
                               'vport': nuage_port['nuage_vport_id']})
                    self.nuageclient.delete_rate_limiting(
                        nuage_port['nuage_vport_id'], fip_id)
                    self.fip_rate_log.info('FIP %s (owned by tenant %s) '
                                           'disassociated from port %s'
                                           % (fip_id, fip['tenant_id'],
                                              port_id))

                router_id = fip['router_id']
            else:
                router_id = fip['last_known_router_id']

            if router_id:
                ent_rtr_mapping = nuagedb.get_ent_rtr_mapping_by_rtrid(
                    context.session,
                    router_id)
                if not ent_rtr_mapping:
                    msg = _('router %s is not associated with '
                            'any net-partition') % router_id
                    raise n_exc.BadRequest(resource='floatingip',
                                       msg=msg)
                params = {
                    'router_id': ent_rtr_mapping['nuage_router_id'],
                    'fip_id': fip_id
                }
                nuage_fip = self.nuageclient.get_nuage_fip_by_id(params)
                if nuage_fip:
                    self.nuageclient.delete_nuage_floatingip(
                        nuage_fip['nuage_fip_id'])
                    LOG.debug('Floating-ip %s deleted from VSD', fip_id)

            super(NuagePlugin, self).delete_floatingip(context, fip_id)
            self.fip_rate_log.info('FIP %s (owned by tenant %s) deleted' %
                                   (fip_id, fip['tenant_id']))

    @nuage_utils.handle_nuage_api_error
    @log.log
    def delete_security_group(self, context, id):
        filters = {'security_group_id': [id]}
        ports = self._get_port_security_group_bindings(context,
                                                       filters)
        if ports:
            raise ext_sg.SecurityGroupInUse(id=id)
        sg_rules = self.get_security_group_rules(context,
                                                 {'security_group_id': [id]})

        if sg_rules:
            self.nuageclient.delete_nuage_sgrule(sg_rules)
        self.nuageclient.delete_nuage_secgroup(id)
        LOG.debug("Deleted security group %s", id)

        super(NuagePlugin, self).delete_security_group(context, id)

    @nuage_utils.handle_nuage_api_error
    @log.log
    def create_security_group_rule(self, context, security_group_rule):
        sg_rule = security_group_rule['security_group_rule']
        self.nuageclient.validate_nuage_sg_rule_definition(sg_rule)
        sg_id = sg_rule['security_group_id']

        local_sg_rule = super(NuagePlugin,
                              self).create_security_group_rule(
                                        context, security_group_rule)

        try:
            nuage_vptag = self.nuageclient.get_sg_vptag_mapping(sg_id)
            if nuage_vptag:
                sg_params = {
                    'sg_id': sg_id,
                    'neutron_sg_rule': local_sg_rule,
                    'vptag': nuage_vptag
                }
                self.nuageclient.create_nuage_sgrule(sg_params)
        except Exception:
            with excutils.save_and_reraise_exception():
                super(NuagePlugin,
                      self).delete_security_group_rule(context,
                                                   local_sg_rule['id'])

        return local_sg_rule

    @nuage_utils.handle_nuage_api_error
    @log.log
    def delete_security_group_rule(self, context, id):
        local_sg_rule = self.get_security_group_rule(context, id)
        super(NuagePlugin, self).delete_security_group_rule(context, id)
        self.nuageclient.delete_nuage_sgrule([local_sg_rule])
        LOG.debug("Deleted security group rule %s", id)

    @nuage_utils.handle_nuage_api_error
    @log.log
    def get_vsd_subnet(self, context, id, fields=None):
        subnet, type = self.nuageclient.get_subnet_or_domain_subnet_by_id(id)
        vsd_subnet = {'id': subnet['subnet_id'],
                      'name': subnet['subnet_name'],
                      'cidr': self._calc_cidr(subnet),
                      'gateway': subnet['subnet_gateway'],
                      'ip_version': subnet['subnet_iptype'],
                      'linked': self._is_subnet_linked(context.session,
                                                       subnet)}
        if type == 'Subnet':
            domain_id = self.nuageclient.get_router_by_domain_subnet_id(
                vsd_subnet['id'])
            netpart_id = self.nuageclient.get_router_np_id(domain_id)
        else:
            netpart_id = subnet['subnet_parent_id']

        net_partition = self.nuageclient.get_net_partition_name_by_id(
            netpart_id)
        vsd_subnet['net_partition'] = net_partition
        return self._fields(vsd_subnet, fields)

    def _calc_cidr(self, subnet):
        if not subnet['subnet_address'] and \
                not subnet['subnet_shared_net_id']:
            return None

        shared_id = subnet['subnet_shared_net_id']
        if shared_id:
            subnet = self.nuageclient.get_nuage_sharedresource(shared_id)
        ip = netaddr.IPNetwork(subnet['subnet_address'] + '/' +
                               subnet['subnet_netmask'])
        return str(ip)

    def _is_subnet_linked(self, session, subnet):
        if subnet['subnet_os_id']:
            return True

        l2dom_mapping = nuagedb.get_subnet_l2dom_by_nuage_id(
            session, subnet['subnet_id'])
        return l2dom_mapping is not None


    @log.log
    def _get_default_net_partition(self, context):
        def_net_part = cfg.CONF.RESTPROXY.default_net_partition_name
        net_partition = nuagedb.get_net_partition_by_name(context.session,
                                                          def_net_part)
        if not net_partition:
            msg = _("Default net_partition is not created at the start")
            raise n_exc.BadRequest(resource='netpartition', msg=msg)
        return net_partition

    @log.log
    def _create_appd_network(self, context, name):
        network = {
            'network': {
                'name': name,
                'router:external': False,
                'provider:physical_network': attributes.ATTR_NOT_SPECIFIED,
                'admin_state_up': True,
                'tenant_id': context.tenant_id,
                'provider:network_type': attributes.ATTR_NOT_SPECIFIED,
                'shared': False,
                'provider:segmentation_id': attributes.ATTR_NOT_SPECIFIED
            }
        }
        binding = None
        (network_type, physical_network,
         vlan_id) = self._process_provider_create(context,
                                                  network['network'])
        with context.session.begin(subtransactions=True):
            self._ensure_default_security_group(
                context,
                network['network']['tenant_id']
            )
            net = super(NuagePlugin, self).create_network(context,
                                                          network)
            self._process_l3_create(context, net, network['network'])
            if network_type == 'vlan':
                binding = nuagedb.add_network_binding(context.session,
                                                      net['id'],
                                                      network_type,
                                                      physical_network,
                                                      vlan_id)
            self._extend_network_dict_provider_nuage(net, None, binding)
            return net

    def _get_appd_network_id(self, appdomain_id):
        application_domain = self.nuageclient.get_nuage_application_domain(
            appdomain_id)
        return application_domain['externalID']

    @log.log
    def _delete_appd_network(self, context, appdomain_id):
        with context.session.begin(subtransactions=True):
            id = self._get_appd_network_id(appdomain_id)
            self._process_l3_delete(context, id)
            filter = {'network_id': [id]}
            subnets = self.get_subnets(context, filters=filter)
            for subnet in subnets:
                LOG.debug("Deleting subnet %s", subnet['id'])
                self.delete_subnet(context, subnet['id'])
            LOG.debug('Deleting network %s', id)
            super(NuagePlugin, self).delete_network(context, id)

    @log.log
    def _make_nuage_application_domain_dict(self, application_router,
                                            context=None, fields=None):
        res = {
            'id': application_router['ID'],
            'name': application_router['name'],
            'applicationDeploymentPolicy': 'ZONE',
        }
        if context:
            res['tenant_id'] = context.tenant_id
        return self._fields(res, fields)

    @log.log
    def _make_nuage_application_dict(self, application, context=None,
                                     fields=None):
        res = {
            'id': application['ID'],
            'name': application['name'],
            'associateddomainid': application['associatedDomainID']
        }
        if context:
            res['tenant_id'] = context.tenant_id
        return self._fields(res, fields)

    @log.log
    def _make_nuage_service_dict(self, service, context=None,
                                 fields=None):
        res = {
            'id': service['ID'],
            'name': service['name'],
            'direction': service['direction'],
            'src_port': service['sourcePort'],
            'dest_port': service['destinationPort'],
            'etherype': service['etherType'],
            'dscp': service['DSCP'],
            'protocol': service['protocol']
        }
        if context:
            res['tenant_id'] = context.tenant_id
        return self._fields(res, fields)

    @log.log
    def _make_nuage_flow_dict(self, flow, context=None,
                              fields=None):
        res = {
            'id': flow['ID'],
            'name': flow['name'],
            'origin_tier': flow['originTierID'],
            'dest_tier': flow['destinationTierID'],
            'application_id': flow['parentID']
        }
        if context:
            res['tenant_id'] = context.tenant_id
        return self._fields(res, fields)

    @log.log
    def _make_nuage_tier_dict(self, tier, context=None,
                              fields=None):
        res = {
            'id': tier['ID'],
            'name': tier['name'],
            'associatedappid': tier['parentID'],
            'type': tier['type'],
        }
        if context:
            res['tenant_id'] = context.tenant_id
        return self._fields(res, fields)

    def _get_neutron_subn_id_for_tier(self, context, name, asppdnet_id):
        filters = {'tenant_id': [context.tenant_id],
                   'name': [name]}
        subnets = self.get_subnets(context, filters=filters)
        for subn in subnets:
            if subn['network_id'] == asppdnet_id:
                return subn

    @log.log
    def _link_nuage_tier(self, context, subnet):
        subn = subnet['subnet']
        nuage_subn_id = subn['nuagenet']
        nuage_tmplt_id = nuage_subn_id
        gw_ip = subn['gateway_ip']
        nuage_netpart_name = subn['net_partition']
        nuage_netpart = nuagedb.get_net_partition_by_name(context.session,
                                                          nuage_netpart_name)
        self._validate_adv_subnet(context, subn, nuage_netpart)

        try:
            with contextlib.nested(lockutils.lock('db-access'),
                                   context.session.begin(
                                           subtransactions=True)):
                neutron_subnet = super(NuagePlugin, self).create_subnet(
                    context, subnet)
                if subn['enable_dhcp']:
                    self._create_port_gateway(context, neutron_subnet, gw_ip)

                subnet_l2dom = nuagedb.add_subnetl2dom_mapping(
                    context.session, neutron_subnet['id'],
                    nuage_subn_id, nuage_netpart['id'],
                    l2dom_id=str(nuage_tmplt_id), managed=True)
        except db_exc.DBError as e:
            if isinstance(e.inner_exception, sql_exc.IntegrityError):
                msg = _("A concurrent binding to the same VSD managed"
                        " subnet detected. This operation is not allowed.")
                raise n_exc.BadRequest(resource='subnet', msg=msg)
        try:
            nuage_npid = nuage_netpart['id']
            (nuage_uid,
             nuage_gid) = self.nuageclient.attach_nuage_group_to_nuagenet(
                 context.tenant, nuage_npid, nuage_subn_id,
                 neutron_subnet.get('shared'))
        except Exception:
            filters = {
                'fixed_ips': {'subnet_id': [neutron_subnet['id']]},
                'device_owner': [constants.DEVICE_OWNER_DHCP_NUAGE]
            }
            gw_ports = self.get_ports(context, filters=filters)
            self._delete_port_gateway(context, gw_ports)
            super(NuagePlugin, self).delete_subnet(context,
                                                   neutron_subnet['id'])
            msg = "Communication with Nuage VSD failed"
            raise n_exc.BadRequest(resource='subnet', msg=msg)

        ns_dict = {}
        ns_dict['nuage_user_id'] = nuage_uid
        ns_dict['nuage_group_id'] = nuage_gid
        with context.session.begin(subtransactions=True):
            nuagedb.update_subnetl2dom_mapping(subnet_l2dom,
                                               ns_dict)

    @log.log
    def _delete_underlying_neutron_subnet(self, context, id):
        subnet = self.get_subnet(context, id)
        filters = {
            'fixed_ips': {'subnet_id': [id]},
            'device_owner': [constants.DEVICE_OWNER_DHCP_NUAGE]
        }
        ports = self.get_ports(context, filters)
        for port in ports:
            if port['device_owner'] != constants.DEVICE_OWNER_DHCP_NUAGE:
                raise n_exc.SubnetInUse(subnet_id=id)
        self._delete_port_gateway(context, ports)

        subnet_l2dom = nuagedb.get_subnet_l2dom_by_id(context.session, id)
        if subnet_l2dom:
            LOG.debug("Found l2domain mapping for subnet %s", id)
            try:
                self.nuageclient.delete_subnet(id)
            except Exception:
                msg = (_('Unable to complete operation on subnet %s.'
                         'One or more ports have an IP allocation '
                         'from this subnet.') % id)
                raise n_exc.BadRequest(resource='subnet', msg=msg)
        super(NuagePlugin, self).delete_subnet(context, id)

        if subnet_l2dom:
            if subnet_l2dom['nuage_managed_subnet']:
                self.nuageclient.detach_nuage_group_to_nuagenet(
                    context.tenant, subnet_l2dom['net_partition_id'],
                    subnet_l2dom['nuage_subnet_id'], subnet['shared'], True)

            if not self._check_router_subnet_for_tenant(
                context, subnet['tenant_id']):
                LOG.debug("No router/subnet found for tenant %s", subnet[
                    'tenant_id'])
                self.nuageclient.delete_user(subnet_l2dom['nuage_user_id'])
                self.nuageclient.delete_group(subnet_l2dom['nuage_group_id'])

    @log.log
    def _create_appdport(self, context, params):
        tier = self.nuageclient.get_nuage_tier(params['tier_id'])
        nuage_app = self.nuageclient.get_nuage_application(tier['parentID'])
        net_id = self._get_appd_network_id(nuage_app['associatedDomainID'])
        neutron_subnet = self._get_neutron_subn_id_for_tier(
            context, tier['name'], net_id)
        port = {
            'port': {
                'status': 'ACTIVE',
                'device_owner': constants.APPD_PORT,
                'binding:vnic_type': 'normal',
                'name': params['name'],
                'binding:host_id': attributes.ATTR_NOT_SPECIFIED,
                'binding:profile': attributes.ATTR_NOT_SPECIFIED,
                'mac_address': attributes.ATTR_NOT_SPECIFIED,
                'network_id': net_id,
                'tenant_id': params['tenant_id'],
                'admin_state_up': True,
                'fixed_ips': [{'subnet_id': neutron_subnet['id'], }],
                'device_id': ''
            }
        }
        port = super(NuagePlugin, self).create_port(context, port)
        return self._extend_port_dict_binding(context, port)

    @nuage_utils.handle_nuage_api_error
    @log.log
    def create_application_domain(self, context, application_domain):
        app_domain = application_domain['application_domain']
        net = self._create_appd_network(context, app_domain['name'])
        net_partition = self._get_default_net_partition(context)
        appdomain_def_templID = self.nuageclient.\
            get_default_appdomain_templateID(net_partition)
        if not appdomain_def_templID:
            appdomain_def_templID = self.nuageclient.\
                create_default_appdomain_template(net_partition)
        params = {
            'net_partition': net_partition,
            'tenant_id': app_domain['tenant_id'],
            'name': app_domain['name'],
            'nuage_domain_template': app_domain.get('nuage_domain_template'),
            'template_id':  appdomain_def_templID,
            'externalID': net['id'],
            'description': app_domain.get('description', '')
        }
        return self.nuageclient.create_nuage_application_domain(params)

    @nuage_utils.handle_nuage_api_error
    @log.log
    def get_application_domain(self, context, id, fields=None):
        application_domain = self.nuageclient.get_nuage_application_domain(id)
        return self._make_nuage_application_domain_dict(
            application_domain, context=context)

    @nuage_utils.handle_nuage_api_error
    @log.log
    def get_application_domains(self, context, filters=None, fields=None):
        net_partition = self._get_default_net_partition(context)
        if 'id' in filters.keys():
            application_domains = (self.nuageclient.
                                   get_nuage_application_domains(
                                   net_partition['id'],
                                   filters['id'][0]))
        elif 'name' in filters.keys():
            application_domains = (self.nuageclient.
                                   get_nuage_application_domains(
                                   net_partition['id'],
                                   filters['name'][0]))
        else:
            application_domains = (self.nuageclient.
                                   get_nuage_application_domains(
                                   net_partition['id']))

        return [self._make_nuage_application_domain_dict(application_domain,
                                                         context, fields)
                for application_domain in application_domains]

    @nuage_utils.handle_nuage_api_error
    @log.log
    def update_application_domain(self, context, id, application_domain):
        app_domain = application_domain['application_domain']
        return self.nuageclient.update_nuage_application_domain(id, app_domain)

    @nuage_utils.handle_nuage_api_error
    @log.log
    def delete_application_domain(self, context, id):
        self._delete_appd_network(context, id)
        self.nuageclient.delete_nuage_application_domain(id)
        net_partition = self._get_default_net_partition(context)
        application_domains = self.nuageclient.get_nuage_application_domains(
            net_partition['id'])
        if not application_domains:
            self.nuageclient.delete_default_appdomain_template(net_partition)

    @nuage_utils.handle_nuage_api_error
    @log.log
    def create_application(self, context, application):
        net_partition = self._get_default_net_partition(context)
        app = application['application']
        params = {
            'net_partition': net_partition,
            'name': app['name'],
            'associatedDomainID': app['applicationdomain_id'],
            'description': app['description']
        }
        return self.nuageclient.create_nuage_application(params)

    @nuage_utils.handle_nuage_api_error
    @log.log
    def get_application(self, context, id, fields=None):
        application = self.nuageclient.get_nuage_application(id)
        return self._make_nuage_application_dict(application, context=context)

    @nuage_utils.handle_nuage_api_error
    @log.log
    def get_applications(self, context, filters=None, fields=None):
        net_partition = self._get_default_net_partition(context)
        if 'id' in filters.keys():
            applications = self.nuageclient.get_nuage_applications(
                net_partition['id'], filters['id'][0])
        elif 'name' in filters.keys():
            applications = self.nuageclient.get_nuage_applications(
                net_partition['id'], filters['name'][0])
        else:
            applications = self.nuageclient.get_nuage_applications(
                net_partition['id'])

        return [self._make_nuage_application_dict(application, context, fields)
                for application in applications]

    @nuage_utils.handle_nuage_api_error
    @log.log
    def delete_application(self, context, id):
        net_partition = self._get_default_net_partition(context)
        std_tiers_in_app = self.nuageclient.get_std_tiers_in_application(id)
        nuage_app = self.nuageclient.get_nuage_application(id)
        net_id = self._get_appd_network_id(nuage_app['associatedDomainID'])
        with context.session.begin(subtransactions=True):
            for tier in std_tiers_in_app:
                neutron_subnet = self._get_neutron_subn_id_for_tier(
                    context, tier['name'], net_id)
                self._delete_underlying_neutron_subnet(
                    context, neutron_subnet['id'])
        self.nuageclient.delete_nuage_application(net_partition['id'], id)

    @nuage_utils.handle_nuage_api_error
    @log.log
    def update_application(self, context, id, application):
        app = application['application']
        return self.nuageclient.update_nuage_application(id, app)

    @nuage_utils.handle_nuage_api_error
    @log.log
    def create_tier(self, context, tier):
        net_partition = self._get_default_net_partition(context)
        subn = tier['tier']
        params = {
            'name': subn['name'],
            'np_id': net_partition['id'],
            'app_id': subn['app_id'],
            'type': subn['type'],
            'fip_pool_id': subn['fip_pool_id'],
            'cidr': subn['cidr']
        }
        nuage_tier = self.nuageclient.create_nuage_tier(params)
        if subn['type'] == constants.TIER_STANDARD:
            nuage_app = self.nuageclient.get_nuage_application(
                nuage_tier['associatedappid'])
            net_id = self._get_appd_network_id(nuage_app['associatedDomainID'])
            subnet = {
                'subnet': {
                    'name': subn['name'],
                    'enable_dhcp': True,
                    'network_id': net_id,
                    'tenant_id': subn['tenant_id'],
                    'net_partition': net_partition['name'],
                    'ip_version': 4,
                    'cidr': subn['cidr'],
                    'nuagenet': nuage_tier['nuage_subnetid'],
                    'gateway_ip': nuage_tier['gateway_ip'],
                    'allocation_pools': attributes.ATTR_NOT_SPECIFIED,
                    'dns_nameservers': attributes.ATTR_NOT_SPECIFIED,
                    'host_routes': attributes.ATTR_NOT_SPECIFIED
                }
            }
            try:
                self._link_nuage_tier(context, subnet)
            except Exception:
                with excutils.save_and_reraise_exception():
                    self.nuageclient.delete_nuage_tier(nuage_tier['id'])
        return nuage_tier

    @nuage_utils.handle_nuage_api_error
    @log.log
    def update_tier(self, context, id, tier):
        nuage_tier = tier['tier']
        orig_tier = self.nuageclient.get_nuage_tier(id)
        if orig_tier['type'] == constants.TIER_STANDARD:
            nuage_app = self.nuageclient.get_nuage_application(
                orig_tier['parentID'])
            net_id = self._get_appd_network_id(nuage_app['associatedDomainID'])
            neutron_subnet = self._get_neutron_subn_id_for_tier(
                context, orig_tier['name'], net_id)
            subnet = {'subnet': {'name': nuage_tier['name']}}
            super(NuagePlugin, self).update_subnet(
                context, neutron_subnet['id'], subnet)
        return self.nuageclient.update_nuage_tier(id, nuage_tier)

    @nuage_utils.handle_nuage_api_error
    @log.log
    def get_tier(self, context, id, fields=None):
        tier = self.nuageclient.get_nuage_tier(id)
        return self._make_nuage_tier_dict(tier, context=context)

    @nuage_utils.handle_nuage_api_error
    @log.log
    def get_tiers(self, context, filters=None, fields=None):
        if 'id' in filters.keys():
            tiers = self.nuageclient.get_nuage_tiers(
                None, id=filters['id'][0])
        elif 'tenant_id' in filters.keys():
            tiers = []
        else:
            tiers = self.nuageclient.get_nuage_tiers(filters['app_id'][0])

        return [self._make_nuage_tier_dict(tier, context, fields)
                for tier in tiers]

    @nuage_utils.handle_nuage_api_error
    @log.log
    def delete_tier(self, context, id):
        macro_name = None
        tier = self.nuageclient.get_nuage_tier(id)
        net_partition = self._get_default_net_partition(context)
        nuage_app = self.nuageclient.get_nuage_application(tier['parentID'])
        net_id = self._get_appd_network_id(nuage_app['associatedDomainID'])
        if not tier:
            raise nuage_exc.NuageNotFound(resource='tier', resource_id=id)
        if tier['type'] == constants.TIER_STANDARD:
            neutron_subnet = self._get_neutron_subn_id_for_tier(
                context, tier['name'], net_id)
            self._delete_underlying_neutron_subnet(context,
                                                   neutron_subnet['id'])
        elif tier['type'] == 'NETWORK_MACRO':
            macro_name = tier['name'] + '_' + tier['ID']
        self.nuageclient.delete_nuage_tier(id)
        if macro_name:
            self.nuageclient.delete_nwmacro_assoc_with_tier(
                macro_name, net_partition['id'])

    @nuage_utils.handle_nuage_api_error
    @log.log
    def create_appdport(self, context, appdport):
        p = appdport['appdport']
        params = {
            'name': p['name'],
            'tier_id': p['tier_id'],
            'tenant_id': p['tenant_id']
        }
        return self._create_appdport(context, params)

    @log.log
    def get_appdports(self, context, filters=None, fields=None):
        filters = {
            'device_owner': [constants.APPD_PORT]
        }
        return self.get_ports(context, filters=filters)

    @nuage_utils.handle_nuage_api_error
    @log.log
    def create_service(self, context, service):
        net_partition = self._get_default_net_partition(context)
        svc = service['service']
        params = {
            'net_partition': net_partition,
            'name': svc['name'],
            'sourcePort': svc['src_port'],
            'destinationPort': svc['dest_port'],
            'protocol': svc['protocol'],
            'ethertype': svc['ethertype'],
            'direction': svc['direction'],
            'dscp': svc['dscp'],
            'description': svc['description']
        }
        nuage_service = self.nuageclient.create_nuage_service(params)
        return nuage_service

    @nuage_utils.handle_nuage_api_error
    @log.log
    def get_service(self, context, id, fields=None):
        service = self.nuageclient.get_nuage_service(id)
        return self._make_nuage_service_dict(service, context=context)

    @nuage_utils.handle_nuage_api_error
    @log.log
    def get_services(self, context, filters=None, fields=None):
        net_partition = self._get_default_net_partition(context)
        if 'id' in filters.keys():
            services = self.nuageclient.get_nuage_services(
                net_partition['id'], filters['id'][0])
        elif 'name' in filters.keys():
            services = self.nuageclient.get_nuage_services(
                net_partition['id'], filters['name'][0])
        else:
            services = self.nuageclient.get_nuage_services(net_partition['id'])

        return [self._make_nuage_service_dict(service, context, fields)
                for service in services]

    @nuage_utils.handle_nuage_api_error
    @log.log
    def delete_service(self, context, id):
        net_partition = self._get_default_net_partition(context)
        self.nuageclient.delete_nuage_service(net_partition['id'], id)

    @nuage_utils.handle_nuage_api_error
    @log.log
    def update_service(self, context, id, service):
        svc = service['service']
        return self.nuageclient.update_nuage_service(id, svc)

    @nuage_utils.handle_nuage_api_error
    @log.log
    def create_flow(self, context, flow):
        nuage_flow = flow['flow']
        net_partition = self._get_default_net_partition(context)
        nuage_app_id = self.nuageclient.get_app_id_of_tier(
            nuage_flow['origin_tier'])
        params = {
            'net_partition': net_partition,
            'name': nuage_flow['name'],
            'originTierID': nuage_flow['origin_tier'],
            'destinationTierID': nuage_flow['dest_tier'],
            'app_id': nuage_app_id,
            'nuage_services': nuage_flow.get('nuage_services'),
            'src_addr_overwrite': nuage_flow.get('src_addr_overwrite'),
            'dest_addr_overwrite': nuage_flow.get('dest_addr_overwrite')
        }

        return self.nuageclient.create_nuage_flow(params)

    @nuage_utils.handle_nuage_api_error
    @log.log
    def get_flow(self, context, id, fields=None):
        flow = self.nuageclient.get_nuage_flow(id)
        return self._make_nuage_flow_dict(flow, context=context)

    @nuage_utils.handle_nuage_api_error
    @log.log
    def get_flows(self, context, filters=None, fields=None):
        if 'id' in filters.keys():
            flows = self.nuageclient.get_nuage_flows(
                None, id=filters['id'][0])
        elif 'tenant_id' in filters.keys():
            flows = []
        else:
            flows = self.nuageclient.get_nuage_flows(filters['app_id'][0])

        return [self._make_nuage_flow_dict(flow, context, fields)
                for flow in flows]

    @nuage_utils.handle_nuage_api_error
    @log.log
    def delete_flow(self, context, id):
        self.nuageclient.delete_nuage_flow(id)

    @nuage_utils.handle_nuage_api_error
    @log.log
    def update_flow(self, context, id, flow):
        return self.nuageclient.update_nuage_flow(id, flow['flow'])
