# -*- coding: utf-8 -*-
# ******************************************************************************
# ZYNTHIAN PROJECT: Zynthian Engine (zynthian_engine_grooveslap)
#
# GrooveSlap Live — Real-time harmonic animation engine for Zynthian
# Thin wrapper that launches the standalone GrooveSlap process and
# communicates via OSC (following the SooperLooper/PureData pattern).
#
# Copyright (C) 2026 Charles Spencer / LoveSlap Recordings
#
# Architecture:
#   This engine wrapper (runs inside Zynthian UI process):
#     - Launches grooveslap_live.py as a separate subprocess
#     - Sends parameter changes via OSC
#     - Receives status updates via OSC
#     - Manages JACK audio routing via zynautoconnect
#
#   The standalone process (runs as its own OS process):
#     - Owns the JACK audio client (RT-safe callback)
#     - Runs beat detection in a background thread
#     - Runs MIDI generation in another background thread
#     - Listens for OSC parameter changes
#
# ******************************************************************************

import os
import logging
from time import sleep
from subprocess import Popen, DEVNULL

import zynautoconnect
from zynconf import ServerPort
from zyngine.zynthian_engine import zynthian_engine
from zyngine.zynthian_signal_manager import zynsigman

# ------------------------------------------------------------------------------
# GrooveSlap Live Engine Class
# ------------------------------------------------------------------------------

# OSC ports — must match grooveslap_live.py defaults
GS_OSC_PORT = 9800        # We send TO the daemon on this port
GS_OSC_REPLY_PORT = 9801  # The daemon sends status back on this port
GS_DIR = "/zynthian/zynthian-sw/grooveslap-live"


class zynthian_engine_grooveslap(zynthian_engine):

    # Instance counter for unique JACK client names
    _instance_count = 0

    # ---------------------------------------------------------------------------
    # Controllers & Screens
    # ---------------------------------------------------------------------------

    _ctrls = [
        ['inversion', {'name': 'inversion', 'value': 0, 'value_min': -6, 'value_max': 6}],
        ['voicing', {'name': 'voicing', 'value': 0, 'value_min': -12, 'value_max': 12}],
        ['velocity', {'name': 'velocity', 'value': 100, 'value_min': 1, 'value_max': 127}],
        ['duration', {'name': 'duration', 'value': 200, 'value_min': 10, 'value_max': 760}],
        ['numerator', {'name': 'numerator', 'value': 4, 'value_min': 1, 'value_max': 16}],
        ['denominator', {'name': 'denominator', 'value': 1, 'value_min': 1, 'value_max': 8}],
        ['inv loop len', {'name': 'inv loop len', 'value': 8, 'value_min': 1, 'value_max': 16}],
        ['voi loop len', {'name': 'voi loop len', 'value': 8, 'value_min': 1, 'value_max': 16}],
        ['inv lfo on', {'name': 'inv lfo on', 'value': 0, 'value_max': 1, 'labels': ['off', 'on']}],
        ['inv lfo wave', {'name': 'inv lfo wave', 'value': 0, 'value_max': 4, 'labels': ['sine', 'tri', 'square', 'ramp_up', 'ramp_dn']}],
        ['inv lfo amp', {'name': 'inv lfo amp', 'value': 50, 'value_min': 0, 'value_max': 100}],

        ['clock source', {'name': 'clock source', 'value': 2, 'value_max': 3, 'labels': ['tight', 'loose', 'internal', 'MIDI clk']}],
        ['bpm', {'name': 'bpm', 'value': 120, 'value_min': 30, 'value_max': 300}],
        ['cycles', {'name': 'cycles', 'value': 4, 'value_min': 1, 'value_max': 24}],
        ['beatspan', {'name': 'beatspan', 'value': 1, 'value_min': 1, 'value_max': 8}],
        ['cc param', {'name': 'cc param', 'value': 0, 'value_min': -40, 'value_max': 127}],
        ['inv lfo num', {'name': 'inv lfo num', 'value': 1, 'value_min': 1, 'value_max': 33}],
        ['inv lfo den', {'name': 'inv lfo den', 'value': 1, 'value_min': 1, 'value_max': 32}],
        ['inv lfo shape', {'name': 'inv lfo shape', 'value': 0, 'value_max': 29}],
        ['dur lfo num', {'name': 'dur lfo num', 'value': 1, 'value_min': 1, 'value_max': 33}],
        ['dur lfo den', {'name': 'dur lfo den', 'value': 1, 'value_min': 1, 'value_max': 32}],
        ['dur lfo shape', {'name': 'dur lfo shape', 'value': 0, 'value_max': 29}],
        ['cc lfo num', {'name': 'cc lfo num', 'value': 1, 'value_min': 1, 'value_max': 33}],
        ['cc lfo den', {'name': 'cc lfo den', 'value': 1, 'value_min': 1, 'value_max': 32}],
        ['cc lfo shape', {'name': 'cc lfo shape', 'value': 0, 'value_max': 29}],
    ]

    _ctrl_screens = [
        ['main', ['inversion', 'duration', 'velocity', 'cc param']],
        ['rhythm', ['cycles', 'beatspan', 'clock source', 'bpm']],
        ['inv lfo', ['inv lfo shape', 'inv lfo num', 'inv lfo den', 'inv lfo amp']],
        ['dur lfo', ['dur lfo shape', 'dur lfo num', 'dur lfo den']],
        ['cc lfo', ['cc lfo shape', 'cc lfo num', 'cc lfo den']],
    ]

    # ---------------------------------------------------------------------------
    # Initialization
    # ---------------------------------------------------------------------------

    def __init__(self, state_manager=None, jackname=None):
        super().__init__(state_manager)
        self.name = "GrooveSlap"
        self.nickname = "GS"

        # Unique JACK client name per instance (grooveslap-01, grooveslap-02, ...)
        zynthian_engine_grooveslap._instance_count += 1
        instance_num = zynthian_engine_grooveslap._instance_count
        self.jackname = f"grooveslap-{instance_num:02d}"

        self.type = "MIDI Tool"

        self.options['replace'] = False
        self.options['clone'] = False
        self.options['drop_pc'] = True
        self.options['note_range'] = False
        self.options['midi_chan'] = True

        self.proc = None
        self.osc_target_port = GS_OSC_PORT
        self.osc_server_port = GS_OSC_REPLY_PORT

        # Status from daemon
        self.current_bpm = 120.0
        self.current_chord_name = "---"
        self.beat_count = 0
        self.click_count = 0

        # Monitors dict for UI widgets
        self.monitors_dict = {
            "bpm": 120.0,
            "chord": "---",
            "beat": 0,
            "click": 0,
            "playing": 0,
        }

        # Build command to launch the standalone daemon
        # Must use system python3, NOT venv — aubio needs numpy <2
        self.command = [
            "/usr/bin/python3",
            os.path.join(GS_DIR, "grooveslap_live.py"),
            "--osc-port", str(GS_OSC_PORT),
            "--osc-reply-port", str(GS_OSC_REPLY_PORT),
            "--jack-name", self.jackname,
        ]

    # ---------------------------------------------------------------------------
    # Subprocess Management
    # ---------------------------------------------------------------------------

    def start(self):
        """Launch the GrooveSlap daemon process."""
        if self.proc and self.proc.poll() is None:
            logging.info("GrooveSlap daemon already running")
            return

        logging.info(f"Starting GrooveSlap daemon: {' '.join(self.command)}")
        try:
            self.proc = Popen(
                self.command,
                stdout=DEVNULL,
                stderr=DEVNULL,
                env=self.command_env,
                cwd=GS_DIR
            )
            # Give the daemon time to create JACK ports and start OSC
            sleep(2.0)
            logging.info(f"GrooveSlap daemon started (PID: {self.proc.pid})")
        except Exception as e:
            logging.error(f"Failed to start GrooveSlap daemon: {e}")
            self.proc = None

        # Initialize OSC communication to the daemon
        self.osc_init()

    def stop(self):
        """Stop the GrooveSlap daemon process."""
        if self.proc:
            try:
                # Try graceful OSC quit first
                if self.osc_target:
                    try:
                        import liblo
                        liblo.send(self.osc_target, "/gs/quit")
                    except:
                        pass
                    sleep(0.5)

                # If still running, terminate
                if self.proc.poll() is None:
                    logging.info("Terminating GrooveSlap daemon")
                    self.proc.terminate()
                    try:
                        self.proc.wait(2.0)
                    except:
                        self.proc.kill()

                self.proc = None
                logging.info("GrooveSlap daemon stopped")
            except Exception as e:
                logging.error(f"Error stopping GrooveSlap daemon: {e}")

        self.osc_end()

    # ---------------------------------------------------------------------------
    # OSC Communication
    # ---------------------------------------------------------------------------

    def osc_add_methods(self):
        """Register OSC message handlers for status updates from daemon."""
        if self.osc_server:
            self.osc_server.add_method("/gs/status/bpm", "f", self._osc_cb_bpm)
            self.osc_server.add_method("/gs/status/chord", "s", self._osc_cb_chord)
            self.osc_server.add_method("/gs/status/beat", "i", self._osc_cb_beat)
            self.osc_server.add_method("/gs/status/click", "i", self._osc_cb_click)
            self.osc_server.add_method("/gs/status/playing", "i", self._osc_cb_playing)
            self.osc_server.add_method("/gs/pong", None, self._osc_cb_pong)
            self.osc_server.add_method(None, None, self._osc_cb_all)

    def _osc_cb_bpm(self, path, args):
        self.current_bpm = args[0]
        self.monitors_dict["bpm"] = args[0]

    def _osc_cb_chord(self, path, args):
        self.current_chord_name = args[0]
        self.monitors_dict["chord"] = args[0]

    def _osc_cb_beat(self, path, args):
        self.beat_count = args[0]
        self.monitors_dict["beat"] = args[0]

    def _osc_cb_click(self, path, args):
        self.click_count = args[0]
        self.monitors_dict["click"] = args[0]

    def _osc_cb_playing(self, path, args):
        self.monitors_dict["playing"] = args[0]

    def _osc_cb_pong(self, path, args, types, src):
        logging.debug("GrooveSlap daemon responded to ping")

    def _osc_cb_all(self, path, args, types, src):
        logging.debug(f"GrooveSlap OSC: {path} {args}")

    def _send_osc(self, path, *args):
        """Send an OSC message to the daemon."""
        if self.osc_target:
            try:
                import liblo
                liblo.send(self.osc_target, path, *args)
            except Exception as e:
                logging.warning(f"OSC send failed: {e}")

    # ---------------------------------------------------------------------------
    # Processor Management
    # ---------------------------------------------------------------------------

    def add_processor(self, processor):
        """Add processor to engine — starts the daemon."""
        super().add_processor(processor)
        if not self.proc or self.proc.poll() is not None:
            self.start()

        # Set MIDI channel from processor
        if processor.midi_chan is not None:
            self._send_osc("/gs/midi_channel", processor.midi_chan)

        # Request MIDI autoconnect (MIDI Tool — routes via ZynMidiRouter)
        zynautoconnect.request_midi_connect(True)

        # Connect audio source for beat detection (gs_capture runs as gs-audio-<jackname>)
        self._connect_audio_input()

    def remove_processor(self, processor):
        """Remove processor from engine — stops daemon if no processors left."""
        super().remove_processor(processor)
        if len(self.processors) == 0:
            self.stop()

    def _connect_audio_input(self):
        """Connect all available audio capture sources to gs_capture for beat detection.

        Connects: USB audio gadget, AirPlay bridge, system capture (HiFiBerry ADC),
        and any hotplug zynain_* devices. All sources are mixed into the single
        mono beat detection input — whichever is playing gets detected.
        """
        import subprocess
        audio_port = f"gs-audio-{self.jackname}:audio_in"
        # All possible audio sources for beat detection
        sources = ["zynain_UAC2Gadget:capture_1", "airplay:capture_1", "system:capture_1"]
        # Also pick up any other zynain_* hotplug devices
        try:
            result = subprocess.run(
                ["jack_lsp"], timeout=2, capture_output=True, text=True
            )
            for line in result.stdout.splitlines():
                if line.startswith("zynain_") and ":capture_1" in line and line not in sources:
                    sources.append(line)
        except Exception:
            pass
        for src in sources:
            try:
                subprocess.run(
                    ["jack_connect", src, audio_port],
                    timeout=2, capture_output=True
                )
                logging.info(f"GrooveSlap audio: {src} -> {audio_port}")
            except Exception as e:
                logging.debug(f"GrooveSlap audio connect {src}: {e}")

    # ---------------------------------------------------------------------------
    # Controller Management
    # ---------------------------------------------------------------------------

    def send_controller_value(self, zctrl):
        """Handle controller value changes from Zynthian UI — send via OSC."""

        osc_map = {
            'inversion':      ("/gs/inversion", "i"),
            'velocity':       ("/gs/velocity", "i"),
            'duration':       ("/gs/duration", "i"),
            'cycles':         ("/gs/cycles", "i"),
            'beatspan':       ("/gs/beatspan", "i"),
            'cc param':       ("/gs/cc_param", "i"),
            'inv lfo amp':    ("/gs/inv_lfo_amp", "i"),
            'inv lfo num':    ("/gs/inv_lfo_num", "i"),
            'inv lfo den':    ("/gs/inv_lfo_den", "i"),
            'inv lfo shape':  ("/gs/inv_lfo_shape", "i"),
            'dur lfo num':    ("/gs/dur_lfo_num", "i"),
            'dur lfo den':    ("/gs/dur_lfo_den", "i"),
            'dur lfo shape':  ("/gs/dur_lfo_shape", "i"),
            'cc lfo num':     ("/gs/cc_lfo_num", "i"),
            'cc lfo den':     ("/gs/cc_lfo_den", "i"),
            'cc lfo shape':   ("/gs/cc_lfo_shape", "i"),
        }

        if zctrl.symbol in osc_map:
            path, typ = osc_map[zctrl.symbol]
            self._send_osc(path, int(zctrl.value))

        elif zctrl.symbol == 'clock source':
            self._send_osc("/gs/clock_source", int(zctrl.value))

        elif zctrl.symbol == 'bpm':
            self._send_osc("/gs/bpm", float(zctrl.value))

    # ---------------------------------------------------------------------------
    # Bank & Preset Management
    # ---------------------------------------------------------------------------

    def get_bank_list(self, processor=None):
        return [('', 0, 'Default', None)]

    def set_bank(self, processor, bank):
        return True

    def get_preset_list(self, bank, processor=None):
        presets = [('default', 0, 'Default Pattern', None)]
        preset_dir = os.path.join(GS_DIR, "presets")
        if os.path.exists(preset_dir):
            import json
            for f in sorted(os.listdir(preset_dir)):
                if f.endswith('.json'):
                    name = f[:-5]
                    fpath = os.path.join(preset_dir, f)
                    presets.append((fpath, len(presets), name, None))
        return presets

    def set_preset(self, processor, preset, preload=False):
        if preset[0] == 'default':
            # Daemon loads default pattern on startup
            pass
        elif os.path.exists(str(preset[0])):
            self._send_osc("/gs/load_preset", str(preset[0]))
        return True

    # ---------------------------------------------------------------------------
    # State Management
    # ---------------------------------------------------------------------------

    def get_state(self, processor):
        state = {
            'bpm': self.current_bpm,
            'chord': self.current_chord_name,
            'beat_count': self.beat_count,
        }
        return state

    def restore_state(self, processor, state):
        # State is mostly runtime — nothing to restore
        pass

    def get_monitors_dict(self):
        return self.monitors_dict

    # ---------------------------------------------------------------------------
    # MIDI Channel Management
    # ---------------------------------------------------------------------------

    def set_midi_chan(self, processor):
        """Update MIDI channel on the daemon."""
        if processor.midi_chan is not None:
            self._send_osc("/gs/midi_channel", processor.midi_chan)
