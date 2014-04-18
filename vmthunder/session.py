#!/usr/bin/env python

import time
import os
import socket
import fcntl
import struct

from oslo.config import cfg

from pydm.common import utils
from vmthunder.openstack.common import log as logging
from vmthunder.drivers import fcg
from vmthunder.drivers import dmsetup
from vmthunder.drivers import iscsi
from vmthunder.drivers import connector
from vmthunder.drivers import volt

CONF = cfg.CONF
LOG = logging.getLogger(__name__)


class Session():
    def __init__(self, volume_name):
        self.volume_name = volume_name
        self.root = None
        self.connections = []
        self.origin = ''
        self.target_path_dict = {}
        self.has_multipath = False
        self.has_cache = False
        self.has_origin = False
        self.has_target = False
        self.is_login = False
        #TODO: all virtual machines called instance
        self.vm = []
        self.peer_id = ''
        self.target_id = 0
        LOG.debug("create a session of volume_name %s" % self.volume_name)

    @staticmethod
    def _get_ip_address(ifname):
        LOG.debug("acquire ip address of %s" % ifname)
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        return socket.inet_ntoa(fcntl.ioctl(
            s.fileno(),
            0x8915,
            struct.pack('256s', ifname[:15]))[20:24])

    def _connection_to_string(self, connection):
        return connection['target_portal'] + connection['target_iqn']

    def _add_target_path_dict(self, connection, path):
        key = self._connection_to_string(connection)
        self.target_path_dict[key] = path

    def _delete_target_path_dict(self, connection):
        key = self._connection_to_string(connection)
        if key in self.target_path_dict.keys():
            del self.target_path_dict[key]

    def _origin_name(self):
        return 'origin_' + self.volume_name

    def _multipath_name(self):
        return 'multipath_' + self.volume_name

    def _multipath(self):
        multipath_name = self._multipath_name()
        multipath = dmsetup.prefix + multipath_name
        return multipath

    def _origin_path(self):
        origin_name = self._origin_name()
        return dmsetup.prefix + origin_name

    def _connection_exits(self, connection):
        if connection in self.connections:
            return True
        else:
            return False

    def _is_connected(self):
        """This method is to judge whether a target is hanging by other VMs"""
        #TODO: try to call brick.iscsi, at least move this tgtadm call to driver.iscsi
        LOG.debug("execute a command of tgtadm to judge a target_id %s whether is hanging" % self.target_id)
        Str = "tgtadm --lld iscsi --mode conn --op show --tid " + str(self.target_id)
        tmp = os.popen(Str).readlines()
        if len(tmp) == 0:
            return False
        return True

    def _judge_target_exist(self, iqn):
        return iscsi.exist(iqn)

    def change_connection_mode(self, connection):
        LOG.debug("old connection is :")
        LOG.debug(connection)
        new_connection = {'target_portal': connection.host + ':' + connection.port,
                          'target_iqn': connection.iqn,
                          'target_lun': connection.lun,
        }
        LOG.debug("new connection is :")
        LOG.debug(new_connection)

        return new_connection

    def _login_target(self, connections):
        """This method is to login target and return the connected_paths
        connection_properties for iSCSI must include:
        target_portal - ip and optional port
        target_iqn - iSCSI Qualified Name
        target_lun - LUN id of the volume_name
        """
        connected_paths = []
        for connection in connections:
            if self._connection_exits(connection) is False:
                LOG.debug("iscsi login target according the connection :")
                LOG.debug(connection)
                device_info = connector.connect_volume(connection)
                path = device_info['path']
                path = os.path.realpath(path)
                self._add_target_path_dict(connection, path)
                connected_paths.append(path)
                self.connections.append(connection)
        return connected_paths

    def _logout_target(self, connection):
        """ parameter device_info is no be used """
        tmp_string = self._connection_to_string(connection)
        if self.target_path_dict.has_key(tmp_string):
            connector.disconnect_volume(connection, '')
            LOG.debug("iscsi logout target according the connection :")
            LOG.debug(connection)
            self._delete_target_path_dict(connection)
            if self._connection_exits(connection):
                self.connections.remove(connection)

    def _create_target(self, iqn, path):
        self.target_id = iscsi.create_iscsi_target(iqn, path)
        LOG.debug("create a target and it's id is %s" % self.target_id)
        self.has_target = True
        #don't dynamic gain host_id and host_port
        #TODO: eth0? br100?
        host_ip = self._get_ip_address('br100')
        LOG.debug("logon to master server")
        #TODO: port? lun?
        info = volt.login(session_name=self.volume_name,
                                peer_id=self.peer_id,
                                host=host_ip,
                                port='3260',
                                iqn=iqn,
                                lun='1')
        self.is_login = True

    def _delete_target(self):
        iscsi.remove_iscsi_target(0, 0, self.volume_name, self.volume_name)
        self.has_target = False
        LOG.debug("successful remove target %s " % self.target_id)

    def _create_multipath(self, disks):
        multipath_name = self._multipath_name()
        multipath_path = dmsetup.multipath(multipath_name, disks)
        self.has_multipath = True
        LOG.debug("create multipath according connection :")
        LOG.debug(disks)
        return multipath_path

    def _delete_multipath(self):
        multipath_name = self._multipath_name()
        dmsetup.remove_table(multipath_name)
        self.has_multipath = False
        LOG.debug("delete multipath of %s" % multipath_name)

    def _create_cache(self, multipath):
        cached_path = fcg.add_disk(multipath)
        self.has_cache = True
        LOG.debug("create cache according to multipath %s" % multipath)
        return cached_path

    def _delete_cache(self, multipath):
        fcg.rm_disk(multipath)
        self.has_cache = False
        LOG.debug("delete cache according to multipath %s " % multipath)

    def _create_origin(self, origin_dev):
        origin_name = self._origin_name()
        origin_path = ''
        if self.has_origin:
            origin_path = self._origin_path()
        else:
            origin_path = dmsetup.origin(origin_name, origin_dev)
            LOG.debug("create origin on %s" % origin_dev)
            self.has_origin = True
        return origin_path

    def _delete_origin(self):
        origin_name = self._origin_name()
        dmsetup.remove_table(origin_name)
        LOG.debug("remove origin %s " % origin_name)
        self.has_origin = False
        self.origin = ''

    def _get_parent(self):
        #TODO: !!!
        host_ip = self._get_ip_address('br100')
        while True:
            self.peer_id, parent_list = volt.get(session_name=self.volume_name, host=host_ip)
            LOG.debug("in get_parent function to get parent_list :")
            LOG.debug(parent_list)
            #TODO: Wait for parents are ready
            bo = True
            for parent in parent_list:
                if parent.status == "pending":
                    bo = False
                    break
            if bo:
                return parent_list
            time.sleep(1)

    def deploy_image(self, connections):
        LOG.debug("come to deploy_image")
        #TODO: Roll back if failed !

        if self.is_login:
            return self.origin

        if isinstance(connections, dict):
            connections = [connections]
        self.root = connections
        parent_list = self._get_parent()
        new_connections = []
        if len(parent_list) == 0:
            #TODO:hanging target from cinder
            new_connections = connections
        else:
            for parent in parent_list:
                new_connections.append(self.change_connection_mode(parent))

        connected_path = self._login_target(new_connections)
        if self.has_multipath:
            self._add_path()
        else:
            multi_path = self._create_multipath(connected_path)
            cached_path = self._create_cache(multi_path)
            connection = connections[0]
            iqn = connection['target_iqn']
            if self._judge_target_exist(iqn) is False:
                self._create_target(iqn, cached_path)
            self._create_origin(cached_path)
        self.origin = self._origin_path()
        return self.origin

    def destroy(self):
        LOG.debug("destroy session")
        if len(self.vm) == 0:
            if self.is_login is True:
                volt.logout(self.volume_name, peer_id=self.peer_id)
                self.is_login = False
            if self.has_target and self._is_connected():
                return False
            self.destroy_for_adjust_structure()
        return True

    def destroy_for_adjust_structure(self):
        multipath = self._multipath()
        if len(self.vm) == 0:
            self._delete_origin()
        if self.has_target:
            self._delete_target()
        time.sleep(1)
        if self.has_origin is False and self.has_target is False:
            self._delete_cache(multipath)
        if self.has_cache is False:
            self._delete_multipath()
        for connection in self.connections:
            self._logout_target(connection)

    def _add_path(self, connections=[]):
        if not connections:
            connections = self.connections
        if len(connections) == 0:
            #TODO:hanging target from cinder
            return
        multipath_name = self._multipath_name()
        key = self._connection_to_string(connections[0])
        size = utils.get_dev_sector_count(self.target_path_dict[key])
        multipath_table = '0 %d multipath 0 0 1 1 queue-length 0 %d 1 ' % (size, len(connections))
        for connection in connections:
            temp = self._connection_to_string(connection)
            multipath_table += self.target_path_dict[temp] + ' 128 '
        multipath_table += '\n'
        LOG.debug('multipath_table is :')
        LOG.debug(multipath_table)
        dmsetup.reload_table(multipath_name, multipath_table)

    def adjust_structure(self, delete_connections, add_connections):
        self._login_target(add_connections)
        for connection in delete_connections:
            if (self._connection_exits(connection)):
                self.connections.remove(connection)
        self._add_path()
        for connection in delete_connections:
            self._logout_target(connection)
        self.destroy_for_adjust_structure()

    def adjust_for_heartbeat(self, connections):
        LOG.debug('adjust_for_heartbeat according connecctions:')
        LOG.debug(connections)

        #If NO parent to connect, connect the root
        new_connections = []
        if not connections:
            new_connections = self.root
        else:
            for connection in connections:
                new_connections.append(self.change_connection_mode(connection))

        for connection in new_connections:
            if self._connection_exits(connection) is False:
                self._login_target([connection])

        self._add_path(new_connections)

        for connection in self.connections:
            if connection not in new_connections:
                self._logout_target(connection)
        self.connections = new_connections

    def has_vm(self):
        if len(self.vm) > 0:
            return True
        else:
            return False

    def add_vm(self, vm_name):
        if vm_name not in self.vm:
            self.vm.append(vm_name)
        else:
            LOG.error("Add vm failed, VM %s existed" % vm_name)

    def rm_vm(self, vm_name):
        try:
            self.vm.remove(vm_name)
        except ValueError:
            LOG.error("remove vm failed. VM %s does not existed" % vm_name)
