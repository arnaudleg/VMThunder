#!/usr/bin/env python

import time
import os

from oslo.config import cfg

from vmthunder.openstack.common import log as logging
from vmthunder.drivers import dmsetup
from vmthunder.drivers import connector
from vmthunder.drivers import fcg

instance_opts = [
    cfg.BoolOpt('snapshot_with_cache',
                default=False,
                help='Whether snapshot can have cache'),
]
CONF = cfg.CONF
CONF.register_opts(instance_opts)

LOG = logging.getLogger(__name__)

iscsi_disk_format = "ip-%s-iscsi-%s-lun-%s"


class Instance():
    def __init__(self, vm_name, session, snapshot_connection):
        self.vm_name = vm_name
        self.connection = snapshot_connection
        self.snapshot_with_cache = CONF.snapshot_with_cache

        snapshot_info = connector.connect_volume(snapshot_connection)
        #TODO: move code fit for openstack outside
        snapshot_link = snapshot_info['path']
        if os.path.exists(snapshot_link):
            self.snapshot_link = snapshot_link
        else:
            raise Exception("Could NOT find snapshot link file %s!" % snapshot_link)

        snapshot_dev = os.path.realpath(self.snapshot_link)
        if os.path.exists(snapshot_dev) or snapshot_dev == snapshot_link:
            self.snapshot_dev = snapshot_dev
        else:
            raise Exception("Could NOT find snapshot device %s!" % snapshot_dev)
        self.session = session
        self.volume_name = session.volume_name
        self.snapshot_path = ''
        self.has_link = False

        LOG.debug("creating a instance of name %s " % self.vm_name)

    def _snapshot_name(self):
        return 'snapshot_' + self.vm_name

    @staticmethod
    def connection_dev(connection):
        return iscsi_disk_format % (connection['target_portal'], connection['target_iqn'], connection['target_lun'])

    def link_snapshot(self):
        target_dev = self.snapshot_link
        os.unlink(target_dev)
        if not os.path.exists(target_dev):
            os.symlink(self.snapshot_path, target_dev)

    def unlink_snapshot(self):
        target_dev = self.snapshot_link
        if os.path.exists(target_dev):
            os.unlink(target_dev)

    def connect_snapshot(self, connection):
        """Connect snapshot volume in cinder server
        """
        return NotImplementedError()

    def _create_cache(self):
        cached_path = fcg.add_disk(self.snapshot_dev)
        return cached_path

    def _delete_cache(self):
        fcg.rm_disk(self.snapshot_dev)

    def _create_snapshot(self, origin_path):
        if self.snapshot_with_cache:
            snap_path = self._create_cache()
        else:
            snap_path = self.snapshot_dev
        snapshot_name = self._snapshot_name()
        snapshot_path = dmsetup.snapshot(origin_path, snapshot_name, snap_path)
        self.snapshot_path = snapshot_path
        return snapshot_path

    def _delete_snapshot(self):
        snapshot_name = self._snapshot_name()
        dmsetup.remove_table(snapshot_name)
        if self.snapshot_with_cache:
            self._delete_cache()

    def start_vm(self, origin_path):
        LOG.debug("VMThunder: start vm %s according origin_path %s" % (self.vm_name, origin_path))
        self._create_snapshot(origin_path)
        self.link_snapshot()
        self.session.add_vm(self.vm_name)
        return self.vm_name

    def del_vm(self):
        LOG.debug("VMThunder: come to instanceSnapCache to delete vm %s" % self.vm_name)
        self._delete_snapshot()
        self.unlink_snapshot()
        self.session.rm_vm(self.vm_name)
    
    def _snapshot_path(self):
        snapshot_name = self._snapshot_name()
        return dmsetup.prefix + snapshot_name