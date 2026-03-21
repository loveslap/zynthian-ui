# -*- coding: utf-8 -*-
# ****************************************************************************
# ZYNTHIAN PROJECT: BeatClock Engine
#
# Audio-to-MIDI-Clock engine. Takes audio input (routed via chain config),
# detects tempo via aubio, outputs 24 PPQN MIDI clock. Connect the clock
# output to GrooveSlap, zynseq, or any clock-consuming engine/device.
#
# Copyright (C) 2026 Charles Spencer / LoveSlap Recordings
#
# ****************************************************************************

import os
import re
import select
import logging
import subprocess
import threading
from time import sleep

from zynautoconnect import zynthian_autoconnect as zynautoconnect
from zyngine.zynthian_engine import zynthian_engine

# ---------------------------------------------------------------------------
# BeatClock Engine Class
# ---------------------------------------------------------------------------


class zynthian_engine_beatclock(zynthian_engine):

    # Controller defaults
    _ctrls = [
        ['bpm', {'name': 'bpm', 'value': 120, 'value_min': 30, 'value_max': 300}],
        ['sensitivity', {'name': 'sensitivity', 'value': 0, 'value_max': 1, 'labels': ['tight', 'loose']}],
        ['playing', {'name': 'playing', 'value': 0, 'value_max': 1, 'labels': ['stopped', 'playing']}],
    ]

    _ctrl_screens = [
        ['main', ['bpm', 'sensitivity', 'playing']],
    ]

    # Engine info
    nickname = "BC"
    jackname = "beatclock"
    engine_type = "Special"

    # ---------------------------------------------------------------------------
    # Initialization
    # ---------------------------------------------------------------------------

    def __init__(self, state_manager=None, jackname=None):
        super().__init__(state_manager)
        self.name = "BeatClock"
        self.nickname = "BC"
        self.type = "Special"

        self.proc = None
        self.jackname = jackname if jackname else "beatclock"
        self._monitor_thread = None
        self._monitor_running = False
        self._last_bpm = 120.0
        self._last_playing = False

        self.custom_gui_fpath = None

        self.options['clone'] = False
        self.options['note_range'] = False
        self.options['audio_route'] = True
        self.options['midi_chan'] = False
        self.options['replace'] = False
        self.options['drop_pc'] = True
        self.options['drop_cc'] = True
        self.options['drop_note'] = True

    # ---------------------------------------------------------------------------
    # Subprocess Management
    # ---------------------------------------------------------------------------

    def start(self):
        """Start the gs_beatclock C process."""
        if self.proc and self.proc.poll() is None:
            return

        gs_dir = "/zynthian/zynthian-sw/grooveslap-live"
        beatclock_bin = os.path.join(gs_dir, "gs_beatclock")

        if not os.path.exists(beatclock_bin):
            logging.error(f"BeatClock: {beatclock_bin} not found")
            return

        jackname = self.jackname

        self.proc = subprocess.Popen(
            [beatclock_bin, "-n", jackname],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            bufsize=0
        )

        # Wait for JACK ports to appear
        for i in range(20):
            sleep(0.25)
            try:
                result = subprocess.run(
                    ["jack_lsp"], timeout=2, capture_output=True, text=True
                )
                if f"{jackname}:audio_in" in result.stdout:
                    logging.info(f"BeatClock: started (PID {self.proc.pid})")
                    self._start_monitor()
                    return
            except Exception:
                pass

        logging.error("BeatClock: JACK ports did not appear")

    def stop(self):
        """Stop the gs_beatclock process and monitor thread."""
        self._monitor_running = False
        if self._monitor_thread:
            self._monitor_thread.join(timeout=2)
            self._monitor_thread = None
        if self.proc:
            try:
                self.proc.terminate()
                self.proc.wait(timeout=5)
            except Exception:
                try:
                    self.proc.kill()
                except Exception:
                    pass
            self.proc = None
            logging.info("BeatClock: stopped")

    def _start_monitor(self):
        """Start background thread to read BPM from process stdout."""
        self._monitor_running = True
        self._monitor_thread = threading.Thread(
            target=self._monitor_loop, daemon=True,
            name="beatclock-monitor")
        self._monitor_thread.start()

    def _monitor_loop(self):
        """Read BPM/status from gs_beatclock stdout and update controllers."""
        while self._monitor_running and self.proc and self.proc.poll() is None:
            try:
                if select.select([self.proc.stdout], [], [], 0.5)[0]:
                    line = self.proc.stdout.readline().decode().strip()
                    if not line:
                        continue
                    # Parse: "BPM:120.0 BEATS:42 PLAYING:1 SENS:0"
                    m_bpm = re.search(r'BPM:([\d.]+)', line)
                    m_play = re.search(r'PLAYING:(\d)', line)
                    if m_bpm:
                        bpm = float(m_bpm.group(1))
                        playing = m_play.group(1) == '1' if m_play else False
                        self._last_bpm = bpm
                        self._last_playing = playing
                        # Update controller values for UI display
                        self._update_controllers(bpm, playing)
            except Exception as e:
                logging.debug(f"BeatClock monitor: {e}")
                sleep(0.5)

    def _update_controllers(self, bpm, playing):
        """Push BPM and playing status to UI controllers."""
        for processor in self.processors:
            try:
                ctrls = processor.controllers_dict
                if 'bpm' in ctrls:
                    ctrls['bpm'].set_value(int(round(bpm)), send=False)
                if 'playing' in ctrls:
                    ctrls['playing'].set_value(1 if playing else 0, send=False)
            except Exception as e:
                logging.debug(f"BeatClock update ctrl: {e}")

    # ---------------------------------------------------------------------------
    # Engine API
    # ---------------------------------------------------------------------------

    def add_processor(self, processor):
        super().add_processor(processor)

        self.start()

        # Request audio autoconnect — chain routing will wire audio input
        zynautoconnect.request_audio_connect(True)

    def remove_processor(self, processor):
        super().remove_processor(processor)
        if len(self.processors) == 0:
            self.stop()

    # ---------------------------------------------------------------------------
    # JACK Name
    # ---------------------------------------------------------------------------

    def get_jackname(self):
        return self.jackname

    # ---------------------------------------------------------------------------
    # Controller Management
    # ---------------------------------------------------------------------------

    def send_controller_value(self, zctrl):
        """Handle controller changes from the UI."""
        # BPM and playing are read-only (display only)
        # sensitivity could be changed via restart with different -s flag
        # For now these are informational
        pass

    def get_controllers_dict(self, processor):
        """Return controller definitions."""
        return super().get_controllers_dict(processor)

    # ---------------------------------------------------------------------------
    # Status Polling
    # ---------------------------------------------------------------------------

    def get_status(self):
        """Read BPM/beat status from stdout pipe."""
        if not self.proc or self.proc.poll() is not None:
            return {"bpm": 0, "playing": False}

        # Non-blocking read from stdout
        try:
            import select
            if select.select([self.proc.stdout], [], [], 0)[0]:
                line = self.proc.stdout.readline().decode().strip()
                # Parse: "BPM:120.0 BEATS:42 PLAYING:1 SENS:0"
                m = re.search(r'BPM:([\d.]+)', line)
                if m:
                    return {
                        "bpm": float(m.group(1)),
                        "playing": "PLAYING:1" in line
                    }
        except Exception:
            pass

        return {"bpm": 0, "playing": False}
