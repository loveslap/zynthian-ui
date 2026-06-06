# -*- coding: utf-8 -*-
# ******************************************************************************
# ZYNTHIAN PROJECT: Zynthian Engine (zynthian_engine_dmx)
#
# Evolux SOG Alien 500mW Laser Controller for Zynthian
# DMX512 audio-reactive lighting engine
#
# Copyright (C) 2026 Charles Spencer / LoveSlap Recordings
#
# Architecture:
#   Wrapper (runs inside Zynthian UI process):
#     - Launches the standalone DMX daemon
#     - Sends parameter changes via OSC
#     - Receives status updates via OSC
#
#   DMX daemon (standalone process):
#     - Owns the DMX512 device /dev/dmx512-0
#     - Runs audio-reactive LFO + envelope modulation
#     - Listens for OSC parameter changes on port 9802
#     - Sends status back on port 9803
#
# ******************************************************************************

import os
import sys
import logging
from time import sleep
from subprocess import Popen, DEVNULL

# Allow direct smoke-testing with /zynthian/venv/bin/python zyngine/zynthian_engine_dmx.py
# while remaining harmless inside the normal Zynthian UI process.
sys.path.insert(0, '/zynthian')
sys.path.insert(0, '/zynthian/zynthian-ui')

from zyncoder.zyncore import lib_zyncore
from zyngine.zynthian_engine import zynthian_engine

# ------ DMX Engine Constants ------

DMX_OSC_PORT = 9802          # We send TO the daemon
DMX_STATUS_PORT = 9803       # The daemon sends status back
DMX_DIR = os.path.join(os.environ.get("ZYNTHIAN_SW_DIR", "/zynthian/zynthian-sw"), "zynthian_engine_dmx")
DMX_SCRIPT = os.path.join(DMX_DIR, "zynthian_engine_dmx.py")


class zynthian_engine_dmx(zynthian_engine):

    _instance_count = 0

    # Force one-detent-per-DMX-value panel behaviour. Zynthian's default
    # nudge factor jumps by 10 for 0..255 integer ranges unless overridden.
    _dmx_byte = {'value_min': 0, 'value_max': 255, 'is_integer': True, 'nudge_factor': 1, 'nudge_factor_fine': 1}
    _dmx_signed = {'value_min': -128, 'value_max': 127, 'is_integer': True, 'nudge_factor': 1, 'nudge_factor_fine': 1}


    # ====== Controllers & Screens ======

    _ctrls = [
        # Main parameters
        ['mode',      dict(_dmx_byte, name='Mode/CH1', value=160)],
        ['color',     dict(_dmx_byte, name='Color/CH2', value=128)],
        ['pattern',   dict(_dmx_byte, name='Pattern/CH3', value=64)],
        ['master',    dict(_dmx_byte, name='Master', value=255)],

        # Raw fixture mapper: choose any DMX channel with one encoder and
        # write a 0-255 value with another. This is for building the real
        # observed fixture profile from the panel, not relying on assumed labels.
        ['raw_chan',   {'name': 'Raw Ch', 'value': 1, 'value_min': 1, 'value_max': 12,
                        'is_integer': True, 'nudge_factor': 1, 'nudge_factor_fine': 1}],
        ['raw_value',  dict(_dmx_byte, name='Raw Byte', value=160)],
        ['raw_signed', dict(_dmx_signed, name='Signed Val', value=32)],

        # Manual center / calibration controls. For the closest 12CH manual we
        # found: CH4=vertical move, CH5=horizontal move, CH8=manual rotation.
        ['center_y',   dict(_dmx_byte, name='Y Center', value=64)],
        ['center_x',   dict(_dmx_byte, name='X Center', value=64)],
        ['center_rot', dict(_dmx_byte, name='Rot Center', value=64)],
        ['cal_apply',  {'name': 'Apply Ctr', 'value': 0, 'value_max': 1,
                        'labels': ['NO', 'YES'], 'ticks': [0, 1]}],

        # DMX channel controls. Names are provisional until the real fixture map
        # is fully measured; every channel is full 0..255 with increment 1.
        ['ch4_zoom',    dict(_dmx_byte, name='CH4/Y', value=0)],
        ['ch5_pan',     dict(_dmx_byte, name='CH5/X', value=0)],
        ['ch6_tilt',    dict(_dmx_byte, name='CH6', value=0)],
        ['ch7_hspeed',  dict(_dmx_byte, name='CH7', value=10)],
        ['ch8_vspeed',  dict(_dmx_byte, name='CH8/Rot', value=10)],
        ['ch9_rotate',  dict(_dmx_byte, name='CH9', value=0)],
        ['ch10_dispspd',dict(_dmx_byte, name='CH10', value=15)],
        ['ch11_strobe', dict(_dmx_byte, name='CH11', value=0)],
        ['ch12_3drot',  dict(_dmx_byte, name='CH12', value=0)],

        # Mode controls
        ['reactive_on', {'name': 'Reactive',  'value': 0,   'value_max': 1, 'labels': ['OFF', 'ON']}],
        ['audio_in',    {'name': 'Audio In',  'value': 0,   'value_max': 3,
                         'labels': ['AirPlay', 'USB Gadget', 'System', 'Off']}],
    ]

    _ctrl_screens = [
        ['main',        ['mode', 'color', 'pattern', 'master']],
        ['raw-map',     ['raw_chan', 'raw_value', 'raw_signed', 'reactive_on']],
        ['calibrate',   ['center_x', 'center_y', 'center_rot', 'cal_apply']],
        ['dmx-ch4-6',   ['ch4_zoom', 'ch5_pan', 'ch6_tilt', 'reactive_on']],
        ['dmx-ch7-10',  ['ch7_hspeed', 'ch8_vspeed', 'ch9_rotate', 'ch10_dispspd']],
        ['dmx-ch11-12', ['ch11_strobe', 'ch12_3drot', 'audio_in']],
    ]

    # ====== Initialization ======

    def __init__(self, state_manager=None, jackname=None):
        super().__init__(state_manager)
        self.name = "DMX Laser"
        self.nickname = "DM"

        zynthian_engine_dmx._instance_count += 1

        self.type = "Special"
        self.options['replace'] = False
        self.options['clone'] = False
        self.options['drop_pc'] = True
        self.options['note_range'] = False
        self.options['midi_chan'] = False

        self.proc = None
        self.osc_target_port = DMX_OSC_PORT
        self.osc_server_port = DMX_STATUS_PORT

        # Raw mapper state for the panel page: one encoder selects channel,
        # another writes the real unsigned DMX byte (0..255), and a third offers
        # a musician-friendly signed view (-128..127) that maps to byte=value+128.
        # Defaults intentionally target CH1=160 because that was the working
        # hello-world/manual-ish mode value.
        self.raw_chan = 1
        self.raw_value = 160
        self.raw_signed = 32
        self.center_x = 64
        self.center_y = 64
        self.center_rot = 64

        self.connected = False
        self.info_line = "DMX: ---"

        # Command to launch daemon
        self.command = ["/usr/bin/python3", DMX_SCRIPT]

    # ====== Subprocess Management ======

    def start(self):
        """Launch the DMX daemon process."""
        if self.proc and self.proc.poll() is None:
            logging.info("DMX daemon already running")
            return

        logging.info(f"Starting DMX daemon: {' '.join(self.command)}")
        try:
            self.proc = Popen(
                self.command,
                stdout=DEVNULL,
                stderr=DEVNULL,
                env=self.command_env,
                cwd=DMX_DIR
            )
            sleep(1.5)
            if self.proc.poll() is None:
                logging.info(f"DMX daemon started (PID: {self.proc.pid})")
                self.connected = True
                self.info_line = "DMX: /dev/dmx512-0"
            else:
                logging.error(f"DMX daemon exited (code: {self.proc.poll()})")
                self.connected = False
                self.info_line = "DMX: FAILED"
                self.proc = None
        except Exception as e:
            logging.error(f"Failed to start DMX daemon: {e}")
            self.connected = False
            self.info_line = "DMX: ERROR"
            self.proc = None

        self.osc_init()

    def stop(self):
        """Stop the DMX daemon process."""
        if self.proc:
            try:
                if self.osc_target:
                    try:
                        import liblo
                        liblo.send(self.osc_target, "/dmx/quit")
                    except:
                        pass
                    sleep(0.5)

                if self.proc.poll() is None:
                    logging.info("Terminating DMX daemon")
                    self.proc.terminate()
                    try:
                        self.proc.wait(2.0)
                    except:
                        self.proc.kill()

                self.proc = None
                self.connected = False
                self.info_line = "DMX: off"
                logging.info("DMX daemon stopped")
            except Exception as e:
                logging.error(f"Error stopping DMX daemon: {e}")

        self.osc_end()

    # ====== OSC Communication ======

    def osc_add_methods(self):
        """Register OSC message handlers for status from daemon."""
        if self.osc_server:
            self.osc_server.add_method("/dmx/status", "s", self._osc_cb_status)
            self.osc_server.add_method("/dmx/device", "s", self._osc_cb_device)
            self.osc_server.add_method("/dmx/pong", None, self._osc_cb_pong)
            self.osc_server.add_method(None, None, self._osc_cb_all)

    def _osc_cb_status(self, path, args):
        self.info_line = f"DMX: {args[0]}"

    def _osc_cb_device(self, path, args):
        self.info_line = f"DMX: {args[0]}"

    def _osc_cb_pong(self, path, args, types, src):
        logging.debug("DMX daemon responded to ping")

    def _osc_cb_all(self, path, args, types, src):
        logging.debug(f"DMX OSC: {path} {args}")

    def _send_osc(self, path, *args):
        """Send OSC message to daemon."""
        if self.osc_target:
            try:
                import liblo
                liblo.send(self.osc_target, path, *args)
            except Exception as e:
                logging.warning(f"OSC send failed: {e}")

    # ====== Processor Management ======

    def add_processor(self, processor):
        """Add processor - starts daemon."""
        super().add_processor(processor)
        if not self.proc or self.proc.poll() is not None:
            self.start()

    def remove_processor(self, processor):
        """Remove processor - stops daemon if last one."""
        super().remove_processor(processor)
        if len(self.processors) == 0:
            self.stop()

    # ====== Controller Management ======

    def send_controller_value(self, zctrl):
        """Handle controller value changes from UI - send via OSC."""

        osc_map = {
            'mode':        ("/dmx/mode", "i"),
            'color':       ("/dmx/color", "i"),
            'pattern':     ("/dmx/pattern", "i"),
            'master':      ("/dmx/master", "i"),
            'ch4_zoom':    ("/dmx/ch4", "i"),
            'ch5_pan':     ("/dmx/ch5", "i"),
            'ch6_tilt':    ("/dmx/ch6", "i"),
            'ch7_hspeed':  ("/dmx/ch7", "i"),
            'ch8_vspeed':  ("/dmx/ch8", "i"),
            'ch9_rotate':  ("/dmx/ch9", "i"),
            'ch10_dispspd':("/dmx/ch10", "i"),
            'ch11_strobe': ("/dmx/ch11", "i"),
            'ch12_3drot':  ("/dmx/ch12", "i"),
        }

        if zctrl.symbol in osc_map:
            path, typ = osc_map[zctrl.symbol]
            self._send_osc(path, int(zctrl.value))
        elif zctrl.symbol == 'raw_chan':
            self.raw_chan = max(1, min(512, int(zctrl.value)))
            self._send_osc(f"/dmx/ch{self.raw_chan}", int(self.raw_value))
        elif zctrl.symbol == 'raw_value':
            self.raw_value = max(0, min(255, int(zctrl.value)))
            self.raw_signed = self.raw_value - 128
            self._send_osc(f"/dmx/ch{self.raw_chan}", int(self.raw_value))
        elif zctrl.symbol == 'raw_signed':
            self.raw_signed = max(-128, min(127, int(zctrl.value)))
            self.raw_value = self.raw_signed + 128
            self._send_osc(f"/dmx/ch{self.raw_chan}", int(self.raw_value))
        elif zctrl.symbol == 'center_x':
            self.center_x = max(0, min(255, int(zctrl.value)))
            self._send_osc("/dmx/ch5", self.center_x)
        elif zctrl.symbol == 'center_y':
            self.center_y = max(0, min(255, int(zctrl.value)))
            self._send_osc("/dmx/ch4", self.center_y)
        elif zctrl.symbol == 'center_rot':
            self.center_rot = max(0, min(255, int(zctrl.value)))
            self._send_osc("/dmx/ch8", self.center_rot)
        elif zctrl.symbol == 'cal_apply':
            if int(zctrl.value):
                self._send_osc("/dmx/reactive", 0)
                self._send_osc("/dmx/mode", 160)
                self._send_osc("/dmx/master", 255)
                self._send_osc("/dmx/ch5", self.center_x)
                self._send_osc("/dmx/ch4", self.center_y)
                self._send_osc("/dmx/ch8", self.center_rot)
        elif zctrl.symbol == 'reactive_on':
            self._send_osc("/dmx/reactive", int(zctrl.value))
        elif zctrl.symbol == 'audio_in':
            self._send_osc("/dmx/audio_input", int(zctrl.value))

    # ====== Bank & Preset Management ======

    def get_bank_list(self, processor=None):
        return [('', 0, 'Default', None)]

    def set_bank(self, processor, bank):
        return True

    def get_preset_list(self, bank, processor=None):
        return [('default', 0, 'Default', None)]

    def set_preset(self, processor, preset, preload=False):
        return True

    # ====== State Management ======

    def get_state(self, processor):
        return {'connected': self.connected}

    def restore_state(self, processor, state):
        pass

    def update_info_display(self):
        """Update the info display line."""
        return True

    def update_display(self, screen_num=0):
        """Update screen display."""
        self.update_info_display()
        return True


if __name__ == "__main__":
    import sys
    logging.basicConfig(level=logging.INFO)
    print("DMX Engine wrapper loaded")
    print(f"DMX_SCRIPT: {DMX_SCRIPT}")
    print(f"DMX_DIR: {DMX_DIR}")
    if os.path.exists(DMX_SCRIPT):
        print("OK: Script exists")
    else:
        print("ERROR: Script not found")
        sys.exit(1)
