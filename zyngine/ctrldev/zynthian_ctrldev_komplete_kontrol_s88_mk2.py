#!/usr/bin/python3
# -*- coding: utf-8 -*-
# ******************************************************************************
# ZYNTHIAN PROJECT: Zynthian Control Device Driver
#
# Komplete Kontrol S88 MK2 HID/display bridge, qKontrol-derived protocol.
#
# This driver intentionally attaches to the S88 MIDI 2 / control port only.
# The S88 MIDI 1 keybed port must remain routed to Zynthian chains.
# ******************************************************************************

import logging
import os
import sys
import threading
import time

# Zynthian specific modules
from zyngine.ctrldev.zynthian_ctrldev_base import zynthian_ctrldev_base

S88_BRIDGE_DIR = "/zynthian/zynthian-custom/controllers/komplete-kontrol-s88-mk2"
if S88_BRIDGE_DIR not in sys.path:
    sys.path.insert(0, S88_BRIDGE_DIR)

try:
    from s88_zynthian_bridge import S88Bridge, text_image
except Exception as e:
    S88Bridge = None
    text_image = None
    logging.error("Can't import S88Bridge from %s => %s", S88_BRIDGE_DIR, e)


class zynthian_ctrldev_komplete_kontrol_s88_mk2(zynthian_ctrldev_base):
    """Native-ish Komplete Kontrol S88 MK2 control-surface bridge.

    v0 scope:
    - Use qKontrol's HID wake/light report so the S88 leaves dim/inert state.
    - Drive both displays with simple Zynthian status screens.
    - Decode HID transport/navigation events and map them to CUIA.
    - Keep this attached to MIDI 2 only; MIDI 1 keybed remains routed.
    """

    dev_ids = ["KOMPLETE KONTROL S88 MK2 MIDI 2"]
    driver_description = "Native HID/display bridge (qKontrol-derived)"
    autoload_flag = False
    unroute_from_chains = True

    CUIA_MAP = {
        "play": "TOGGLE_AUDIO_PLAY",
        "stop": "STOP_AUDIO_PLAY",
        "record": "TOGGLE_AUDIO_RECORD",
        "click": "SCREEN_ALSA_MIXER",
        "clear": "ALL_NOTES_OFF",
        "preset_up": "ARROW_UP",
        "preset_down": "ARROW_DOWN",
        "page_left_or_device_minus": "ARROW_LEFT",
        "page_right_or_device_plus": "ARROW_RIGHT",
    }

    def __init__(self, state_manager, idev_in, idev_out=None):
        super().__init__(state_manager, idev_in, idev_out)
        self.bridge = None
        self._stop = threading.Event()
        self._thread = None
        self._last_display_update = 0

    def init(self):
        if S88Bridge is None:
            logging.error("Komplete Kontrol S88 MK2 bridge unavailable; not starting")
            return
        try:
            self.bridge = S88Bridge()
            self.bridge.wake()
            self.refresh()
            self._thread = threading.Thread(target=self._hid_loop, name="s88-hid-loop", daemon=True)
            self._thread.start()
            logging.info("Komplete Kontrol S88 MK2 HID/display bridge started")
        except Exception as e:
            logging.exception("Can't initialize Komplete Kontrol S88 MK2 bridge => %s", e)
        super().init()

    def end(self):
        self._stop.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=1.0)
        if self.bridge:
            try:
                self.bridge.close_hid()
            except Exception:
                pass
        super().end()
        logging.info("Komplete Kontrol S88 MK2 HID/display bridge stopped")

    def midi_event(self, ev):
        # MIDI 2 handling is intentionally empty for v0. The useful controls are HID.
        return False

    def refresh(self):
        if not self.bridge or text_image is None:
            return
        now = time.monotonic()
        if now - self._last_display_update < 0.5:
            return
        self._last_display_update = now
        try:
            title, preset = self._get_active_context()
            left = text_image(
                "S88 ↔ Zynthian",
                [
                    f"Chain: {title}",
                    f"Preset: {preset}",
                    "Keys: MIDI 1 routed to chains",
                    "HID: Play Stop Rec Clear",
                ],
                (255, 0, 255),
            )
            right = text_image(
                "Controls v0",
                [
                    "Clear  → All Notes Off",
                    "Play   → Audio Play",
                    "Stop   → Audio Stop",
                    "Record → Audio Record",
                    "Pages  → Left / Right",
                ],
                (0, 255, 255),
            )
            self.bridge.send_display(0, left)
            self.bridge.send_display(1, right)
        except Exception as e:
            logging.warning("S88 display refresh failed => %s", e)

    def _get_active_context(self):
        title = "-"
        preset = "-"
        try:
            chain = self.chain_manager.get_active_chain()
            if chain:
                try:
                    title = chain.get_title()
                except Exception:
                    title = str(getattr(chain, "chain_id", "active"))
                try:
                    proc = chain.get_current_processor()
                    if proc:
                        preset = getattr(proc, "preset_name", None) or getattr(proc, "bank_name", None) or "-"
                except Exception:
                    pass
        except Exception:
            pass
        return title, preset

    def _hid_loop(self):
        try:
            self.bridge.open_hid(False)
            for event in self.bridge.events(None):
                if self._stop.is_set():
                    break
                self._handle_hid_event(event)
        except Exception as e:
            if not self._stop.is_set():
                logging.exception("S88 HID loop failed => %s", e)
        finally:
            try:
                self.bridge.close_hid()
            except Exception:
                pass

    def _handle_hid_event(self, event):
        logging.debug("S88 HID event kind=%s name=%s value=%s", event.kind, event.name, event.value)
        if event.kind == "button":
            cuia = self.CUIA_MAP.get(event.name)
            if cuia:
                self.state_manager.send_cuia(cuia)
                self.refresh()
        elif event.kind == "encoder":
            # Coarse v0 mapping: encoder state acts as UI navigation.
            cuia = {
                "encoder_state_0x20": "ARROW_LEFT",
                "encoder_state_0x40": "ARROW_RIGHT",
                "encoder_state_0x80": "ARROW_UP",
                "encoder_state_0xc0": "ARROW_DOWN",
            }.get(event.name)
            if cuia:
                self.state_manager.send_cuia(cuia)
        elif event.kind == "knob":
            # v0: prove decoding only. Parameter control comes next after deciding active-chain API.
            pass
