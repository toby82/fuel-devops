import os
import sys
import stat
import tempfile
import shutil
import urllib
import ipaddr
import glob
import random
import string
import re

from devops.model import Node, Network
from devops.network import IpNetworksPool
from devops.error import DevopsError
from devops import my_yaml

import logging
logger = logging.getLogger('devops.controller')

def randstr(length=8):
    return ''.join(random.choice(string.ascii_letters) for i in xrange(length))


class Controller:
    def __init__(self, driver):
        self.driver = driver

        self.networks_pool = IpNetworksPool()
        self._reserve_networks()

        self.home_dir = os.environ.get('DEVOPS_HOME') or os.path.join(os.environ['HOME'], ".devops")
        try:
            os.makedirs(os.path.join(self.home_dir, 'environments'), 0755)
        except OSError:
            sys.exc_clear()

    def build_environment(self, environment):
        logger.info("Building environment %s" % environment.name)

        env_id = getattr(environment, 'id', '-'.join([environment.name, randstr()]))
        environment.id = env_id

        logger.debug("Creating environment working directory for %s environment" % environment.name)
        environment.work_dir = os.path.join(self.home_dir, 'environments', environment.id)
        os.mkdir(environment.work_dir, stat.S_IRWXU | stat.S_IRWXG | stat.S_IRWXO)
        logger.debug("Environment working directory has been created: %s" % environment.work_dir)

        environment.driver = self.driver

        for node in environment.nodes:
            if node.cdrom:
                path = node.cdrom.isopath
                if path.find('://') == -1:
                    continue
                logger.debug("Caching iso file for node %s from %s" % (node.name, node.cdrom.isopath))
                node.cdrom.isopath = self._cache_file(node.cdrom.isopath)

        for network in environment.networks:
            logger.info("Building network %s" % network.name)

            network.ip_addresses = self.networks_pool.get()
            self.driver.create_network(network)
            network.driver = self.driver
            network.start()

        for node in environment.nodes:
            logger.info("Building node %s" % node.name)

            self._build_node(environment, node)
            node.driver = self.driver

        environment.built = True
        logger.info("Finished building environment %s" % environment.name)

    def destroy_environment(self, environment):
        logger.info("Destroying environment %s" % environment.name)

        for node in environment.nodes:
            logger.info("Destroying node %s" % node.name)

            node.stop()
            self.driver.delete_node(node)
            del node.driver

        for network in environment.networks:
            logger.info("Destroying network %s" % network.name)

            network.stop()
            self.driver.delete_network(network)
            del network.driver
            
            # FIXME
            try:
                self.networks_pool.put(network.ip_addresses)
            except:
                pass

        del environment.driver

        logger.info("Removing environment %s files" % environment.name)

        shutil.rmtree(environment.work_dir)

        logger.info("Finished destroying environment %s" % environment.name)

    def load_environment(self, environment_id):
        env_work_dir = os.path.join(self.home_dir, 'environments', environment_id)
        env_config_file = os.path.join(env_work_dir, 'config')
        if not os.path.exists(env_config_file):
            raise DevopsError, "Environment '%s' couldn't be found" % environment_id

        with file(env_config_file) as f:
            data = f.read()

        environment = my_yaml.load(data)

        return environment

    def save_environment(self, environment):
        data = my_yaml.dump(environment)
        if not environment.built:
            raise DevopsError, "Environment has not been built yet."
        with file(os.path.join(environment.work_dir, 'config'), 'w') as f:
            f.write(data)

    @property
    def saved_environments(self):
        saved_environments = []
        for path in glob.glob(os.path.join(self.home_dir, 'environments', '*')):
            if os.path.exists(os.path.join(path, 'config')):
                saved_environments.append(os.path.basename(path))
        return saved_environments

    def _reserve_networks(self):
        logger.debug("Scanning for ip networks that are already taken")
        with os.popen("ip route") as f:
            for line in f:
                words = line.split()
                if len(words) == 0:
                    continue
                if words[0] == 'default':
                    continue
                address = ipaddr.IPv4Network(words[0])
                logger.debug("Reserving ip network %s" % address)
                self.networks_pool.reserve(address)

        logger.debug("Finished scanning for taken ip networks")

    def _build_network(self, environment, network):
        network.ip_addresses = self.networks_pool.get()

        self.driver.create_network(network)

    def _build_node(self, environment, node):
        for disk in filter(lambda d: d.path is None, node.disks):
            logger.debug("Creating disk file for node '%s'" % node.name)
            fd, disk.path = tempfile.mkstemp(
                prefix=environment.work_dir + '/disk',
                suffix='.' + disk.format
            )
            os.close(fd)
            os.chmod(disk.path, stat.S_IRUSR | stat.S_IWUSR | stat.S_IRGRP | stat.S_IWGRP | stat.S_IROTH)
            self.driver.create_disk(disk)

        logger.debug("Creating node '%s'" % node.name)
        self.driver.create_node(node)

    def _cache_file(self, url):
        cache_dir = os.path.join(self.home_dir, 'cache')
        if not os.path.exists(cache_dir):
            os.makedirs(cache_dir, 0755)

        cache_log_path = os.path.join(cache_dir, 'entries')
        if os.path.exists(cache_log_path):
            with file(cache_log_path) as f:
                cache_entries = my_yaml.load(f.read())
        else:
            cache_entries = dict()

        if cache_entries.has_key(url):
            logger.debug("Cache hit for '%s': '%s'" % (url, cache_entries[url]))
            return cache_entries[url]

        logger.debug("Cache miss for '%s', downloading")

        fd, cached_path = tempfile.mkstemp(prefix=cache_dir+'/')
        os.close(fd)

        urllib.urlretrieve(url, cached_path)

        cache_entries[url] = cached_path

        with file(cache_log_path, 'w') as f:
            f.write(my_yaml.dump(cache_entries))

        logger.debug("Cached '%s' to '%s'" % (url, cached_path))

        return cached_path

