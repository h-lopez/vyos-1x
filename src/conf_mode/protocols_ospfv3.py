#!/usr/bin/env python3
#
# Copyright (C) 2021 VyOS maintainers and contributors
#
# This program is free software; you can redistribute it and/or modify
# it under the terms of the GNU General Public License version 2 or later as
# published by the Free Software Foundation.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <http://www.gnu.org/licenses/>.

import os

from sys import exit
from sys import argv

from vyos.config import Config
from vyos.configdict import dict_merge
from vyos.configdict import node_changed
from vyos.configverify import verify_common_route_maps
from vyos.template import render_to_string
from vyos.ifconfig import Interface
from vyos.util import get_interface_config
from vyos.xml import defaults
from vyos import ConfigError
from vyos import frr
from vyos import airbag
airbag.enable()

def get_config(config=None):
    if config:
        conf = config
    else:
        conf = Config()

    vrf = None
    if len(argv) > 1:
        vrf = argv[1]

    base_path = ['protocols', 'ospfv3']

    # eqivalent of the C foo ? 'a' : 'b' statement
    base = vrf and ['vrf', 'name', vrf, 'protocols', 'ospfv3'] or base_path
    ospfv3 = conf.get_config_dict(base, key_mangling=('-', '_'), get_first_key=True)

    # Assign the name of our VRF context. This MUST be done before the return
    # statement below, else on deletion we will delete the default instance
    # instead of the VRF instance.
    if vrf: ospfv3['vrf'] = vrf

    # FRR has VRF support for different routing daemons. As interfaces belong
    # to VRFs - or the global VRF, we need to check for changed interfaces so
    # that they will be properly rendered for the FRR config. Also this eases
    # removal of interfaces from the running configuration.
    interfaces_removed = node_changed(conf, base + ['interface'])
    if interfaces_removed:
        ospfv3['interface_removed'] = list(interfaces_removed)

    # Bail out early if configuration tree does not exist
    if not conf.exists(base):
        ospfv3.update({'deleted' : ''})
        return ospfv3

    # We also need some additional information from the config, prefix-lists
    # and route-maps for instance. They will be used in verify().
    #
    # XXX: one MUST always call this without the key_mangling() option! See
    # vyos.configverify.verify_common_route_maps() for more information.
    tmp = conf.get_config_dict(['policy'])
    # Merge policy dict into "regular" config dict
    ospfv3 = dict_merge(tmp, ospfv3)

    return ospfv3

def verify(ospfv3):
    if not ospfv3:
        return None

    verify_common_route_maps(ospfv3)

    if 'interface' in ospfv3:
        for interface, interface_config in ospfv3['interface'].items():
            if 'ifmtu' in interface_config:
                mtu = Interface(interface).get_mtu()
                if int(interface_config['ifmtu']) > int(mtu):
                    raise ConfigError(f'OSPFv3 ifmtu can not exceed physical MTU of "{mtu}"')

            # If interface specific options are set, we must ensure that the
            # interface is bound to our requesting VRF. Due to the VyOS
            # priorities the interface is bound to the VRF after creation of
            # the VRF itself, and before any routing protocol is configured.
            if 'vrf' in ospfv3:
                vrf = ospfv3['vrf']
                tmp = get_interface_config(interface)
                if 'master' not in tmp or tmp['master'] != vrf:
                    raise ConfigError(f'Interface {interface} is not a member of VRF {vrf}!')

    return None

def generate(ospfv3):
    if not ospfv3 or 'deleted' in ospfv3:
        return None

    ospfv3['new_frr_config'] = render_to_string('frr/ospf6d.frr.tmpl', ospfv3)
    return None

def apply(ospfv3):
    ospf6_daemon = 'ospf6d'

    # Save original configuration prior to starting any commit actions
    frr_cfg = frr.FRRConfig()

    # Generate empty helper string which can be ammended to FRR commands, it
    # will be either empty (default VRF) or contain the "vrf <name" statement
    vrf = ''
    if 'vrf' in ospfv3:
        vrf = ' vrf ' + ospfv3['vrf']

    frr_cfg.load_configuration(ospf6_daemon)
    frr_cfg.modify_section(f'^router ospf6{vrf}', stop_pattern='^exit', remove_stop_mark=True)

    for key in ['interface', 'interface_removed']:
        if key not in ospfv3:
            continue
        for interface in ospfv3[key]:
            frr_cfg.modify_section(f'^interface {interface}{vrf}', stop_pattern='^exit', remove_stop_mark=True)

    if 'new_frr_config' in ospfv3:
        frr_cfg.add_before(frr.default_add_before, ospfv3['new_frr_config'])

    frr_cfg.commit_configuration(ospf6_daemon)

    return None

if __name__ == '__main__':
    try:
        c = get_config()
        verify(c)
        generate(c)
        apply(c)
    except ConfigError as e:
        print(e)
        exit(1)
