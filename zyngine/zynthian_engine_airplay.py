# -*- coding: utf-8 -*-
# ******************************************************************************
# ZYNTHIAN PROJECT: Zynthian Engine (zynthian_engine_airplay)
#
# AirPlay Audio Receiver — puts AirPlay audio on a mixer channel.
# Uses the existing shairport-sync + alsa_in bridge (airplay JACK client).
# No subprocess to manage — just routes the existing JACK ports.
#
# Copyright (C) 2026 Charles Spencer / LoveSlap Recordings
# ******************************************************************************

import logging

import zynautoconnect
from zyngine.zynthian_engine import zynthian_engine

# ------------------------------------------------------------------------------
# AirPlay Engine Class
# ------------------------------------------------------------------------------

class zynthian_engine_airplay(zynthian_engine):

    # ---------------------------------------------------------------------------
    # Controllers & Screens
    # ---------------------------------------------------------------------------

    _ctrls = []
    _ctrl_screens = []

    # ---------------------------------------------------------------------------
    # Initialization
    # ---------------------------------------------------------------------------

    def __init__(self, state_manager=None, jackname=None):
        super().__init__(state_manager)
        self.name = "AirPlay"
        self.nickname = "AR"
        self.jackname = "airplay"
        self.type = "Audio Generator"

        self.options['replace'] = False
        self.options['clone'] = False
        self.options['drop_pc'] = True
        self.options['note_range'] = False
        self.options['audio_route'] = True
        self.options['midi_chan'] = False

    # ---------------------------------------------------------------------------
    # Subprocess Management — nothing to launch, bridge is a system service
    # ---------------------------------------------------------------------------

    def start(self):
        """Nothing to start — shairport-sync and airplay-bridge are systemd services."""
        logging.info("AirPlay engine: using existing airplay JACK client")

    def stop(self):
        """Nothing to stop — services keep running."""
        pass

    # ---------------------------------------------------------------------------
    # Processor Management
    # ---------------------------------------------------------------------------

    def add_processor(self, processor):
        super().add_processor(processor)
        zynautoconnect.request_audio_connect(True)
        logging.info("AirPlay processor added — audio routed to mixer channel")

    def remove_processor(self, processor):
        super().remove_processor(processor)

    # ---------------------------------------------------------------------------
    # Bank & Preset (none needed)
    # ---------------------------------------------------------------------------

    def get_bank_list(self, processor=None):
        return [('', 0, 'AirPlay Receiver', None)]

    def set_bank(self, processor, bank):
        return True

    def get_preset_list(self, bank, processor=None):
        return [('default', 0, 'AirPlay Audio', None)]

    def set_preset(self, processor, preset, preload=False):
        return True

    # ---------------------------------------------------------------------------
    # No state to manage
    # ---------------------------------------------------------------------------

    def get_state(self, processor):
        return {}

    def restore_state(self, processor, state):
        pass
