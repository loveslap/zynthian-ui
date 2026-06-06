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
import re
import sys
import threading
import time

# Zynthian specific modules
from zyngine.ctrldev.zynthian_ctrldev_base import zynthian_ctrldev_base

S88_BRIDGE_DIR = "/zynthian/zynthian-custom/controllers/komplete-kontrol-s88-mk2"
if S88_BRIDGE_DIR not in sys.path:
    sys.path.insert(0, S88_BRIDGE_DIR)

try:
    from s88_zynthian_bridge import S88Bridge, text_image, TOP_BUTTON_CC, TOP_KNOB_CC
except Exception as e:
    S88Bridge = None
    text_image = None
    TOP_BUTTON_CC = [102, 103, 104, 105, 106, 107, 108, 109]
    TOP_KNOB_CC = [70, 71, 72, 73, 74, 75, 76, 77]
    logging.error("Can't import S88Bridge from %s => %s", S88_BRIDGE_DIR, e)


class zynthian_ctrldev_komplete_kontrol_s88_mk2(zynthian_ctrldev_base):
    """Native-ish Komplete Kontrol S88 MK2 control-surface bridge.

    v0 scope:
    - Use qKontrol's HID wake/light report so the S88 leaves dim/inert state.
    - Drive both displays with simple Zynthian status screens.
    - Decode HID transport/navigation events and map them to CUIA.
    - Keep this attached to MIDI 2 only; MIDI 1 keybed remains routed.
    """

    dev_ids = ["KOMPLETE KONTROL S88 MK2 MIDI 2", "KOMPLETE KONTROL S88 MK2 IN 2"]
    driver_description = "Native HID/display bridge (qKontrol-derived)"
    # Safe to autoload: this exact dev_id is the S88 MIDI 2 control port, not the keybed.
    autoload_flag = True
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

    TOP_BUTTON_ACTIONS = {
        0: "BANK_PREV",
        1: "BANK_NEXT",
        2: "ARROW_LEFT",
        3: "ARROW_RIGHT",
        4: "ARROW_UP",
        5: "ARROW_DOWN",
        6: "DISPLAY_PAGE",
        7: "ALL_NOTES_OFF",
    }

    def __init__(self, state_manager, idev_in, idev_out=None):
        super().__init__(state_manager, idev_in, idev_out)
        self.bridge = None
        self._stop = threading.Event()
        self._thread = None
        self._last_display_update = 0
        self._param_bank = 0
        self._display_page = 0
        self._display_page_count = 3
        self._last_knobs8 = None
        self._last_event = "boot"
        self._last_cuia = "-"
        self._last_action_time = time.monotonic()
        self._last_param_display_refresh = 0

    def init(self):
        if S88Bridge is None:
            logging.error("Komplete Kontrol S88 MK2 bridge unavailable; not starting")
            return
        try:
            self.bridge = S88Bridge()
            self._apply_button_lights(['plugin', 'midi', 'loop'])
            self.refresh(force=True)
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
        evtype = (ev[0] >> 4) & 0x0F
        if evtype == 0xB:
            ccnum = ev[1] & 0x7F
            ccval = ev[2] & 0x7F
            if ccnum in TOP_BUTTON_CC:
                if ccval > 0:
                    index = TOP_BUTTON_CC.index(ccnum)
                    action = self.TOP_BUTTON_ACTIONS.get(index)
                    self._handle_top_button(index, action, ccval)
                return True
            if ccnum in TOP_KNOB_CC:
                index = TOP_KNOB_CC.index(ccnum)
                zctrls = self._get_bank_zctrls()
                if index < len(zctrls):
                    self._set_zctrl_from_7bit(zctrls[index], ccval)
                    self._last_event = f"midi-cc:knob{index + 1}={ccval}"
                    self._last_cuia = "PARAM_CC"
                    self._last_action_time = time.monotonic()
                    self._refresh_after_param_change()
                    return True
        return False

    def _handle_top_button(self, index, action, value=127):
        self._last_event = f"top{index + 1}:{action}={value}"
        self._last_cuia = action or "-"
        self._last_action_time = time.monotonic()
        if action == "BANK_PREV":
            self._change_param_bank(-1)
        elif action == "BANK_NEXT":
            self._change_param_bank(1)
        elif action == "DISPLAY_PAGE":
            self._cycle_display_page(1)
        elif action:
            self._send_cuia(action)
            self.refresh(force=True)
        else:
            self.refresh(force=True)

    def refresh(self, force=False):
        if not self.bridge or text_image is None:
            return
        now = time.monotonic()
        if not force and now - self._last_display_update < 0.5:
            return
        self._last_display_update = now
        started = time.monotonic()
        try:
            if self._display_page == 1:
                left_title, left_lines, right_title, right_lines = self._build_transport_page()
                accent = (255, 180, 0)
            elif self._display_page == 2:
                left_title, left_lines, right_title, right_lines = self._build_routing_page()
                accent = (120, 180, 255)
            else:
                left_title, left_lines, right_title, right_lines = self._build_param_page()
                accent = (0, 255, 255)
            self.bridge.send_display(0, text_image(left_title, left_lines, accent))
            self.bridge.send_display(1, text_image(right_title, right_lines, accent))
            elapsed = time.monotonic() - started
            if elapsed > 0.15:
                logging.debug("S88 display refresh took %.3fs", elapsed)
        except Exception as e:
            logging.warning("S88 display refresh failed => %s", e)

    def _refresh_after_param_change(self):
        """Keep knob-to-Zynthian control immediate by not repainting per HID frame.

        A full S88 repaint pushes two ~261 KB USB bitmap transfers. Doing that for
        every knob report starves the HID/control loop and makes the Zynthian UI
        feel seconds behind. Parameter writes are the real-time path; the S88
        display is only an occasional status mirror during continuous knob motion.
        """
        now = time.monotonic()
        if now - self._last_param_display_refresh < 1.0:
            return
        self._last_param_display_refresh = now
        self.refresh(force=False)

    def _build_param_page(self):
        title, preset, proc_name = self._get_active_context()
        zctrls = self._get_bank_zctrls()
        left_lines = [
            f"Chain: {title}",
            f"Preset: {preset}",
            f"Proc: {proc_name}",
            f"Bank: {self._param_bank + 1}",
        ]
        right_lines = []
        if zctrls:
            for i, zctrl in enumerate(zctrls[:4]):
                left_lines.append(f"K{i + 1}: {self._format_zctrl(zctrl)}")
            for i, zctrl in enumerate(zctrls[4:8], start=5):
                right_lines.append(f"K{i}: {self._format_zctrl(zctrl)}")
        else:
            left_lines.append("No active params")
            right_lines.append("Add/select a chain")
        right_lines.extend([
            "Top CCs: system-owned",
            "Knob MIDI CCs: off",
            self._event_status_line(),
        ])
        controls = self._top_button_legend()
        return controls, left_lines, controls, right_lines

    def _build_transport_page(self):
        left_lines = [
            "Play: toggle audio play",
            "Stop: stop audio play",
            "Record: toggle audio rec",
            "Clear: all notes off",
            "Click: ALSA mixer screen",
            "Loop: display page",
            self._event_status_line(),
        ]
        right_lines = [
            "4D / encoder:",
            "  Left/Right arrows",
            "  Up/Down arrows",
            "Preset +/-: up/down",
            "Plugin/MIDI: param bank",
            f"Param bank: {self._param_bank + 1}",
            f"Last CUIA: {self._last_cuia}",
        ]
        return self._top_button_legend(), left_lines, self._top_button_legend(), right_lines

    def _build_routing_page(self):
        left_lines = [
            "MIDI 1: keybed -> chains",
            "MIDI 2: ctrldev target",
            "HID: buttons/encoder/knobs",
            "USB IF3: dual displays",
            "No Mackie dependency",
            "No NI host required",
            self._event_status_line(),
        ]
        right_lines = [
            "Clock rig target:",
            "Hapax OR BeatClock master",
            "TR-8S follows master",
            "Avoid clock loops",
            "S88 controls active chain",
            "MIDI 1 must stay playable",
            f"Page {self._display_page + 1}/{self._display_page_count}",
        ]
        return self._top_button_legend(), left_lines, self._top_button_legend(), right_lines

    def _top_button_legend(self):
        return "Top: Bank- Bank+  ←  →  ↑  ↓  Page Panic"

    def _remember_event(self, event, cuia="-"):
        self._last_event = f"{event.kind}:{event.name}={event.value}"
        self._last_cuia = cuia or "-"
        self._last_action_time = time.monotonic()

    def _event_status_line(self):
        age = max(0.0, time.monotonic() - self._last_action_time)
        return f"Last: {self._last_event[:26]} {age:.1f}s"

    def _send_cuia(self, cuia):
        self._last_cuia = cuia or "-"
        self.state_manager.send_cuia(cuia)

    def _cycle_display_page(self, delta=1):
        self._display_page = (self._display_page + delta) % self._display_page_count
        active = ['loop']
        if self._display_page == 0:
            active.extend(['plugin', 'midi'])
        elif self._display_page == 1:
            active.extend(['play', 'stop', 'record'])
        elif self._display_page == 2:
            active.extend(['page_left', 'page_right'])
        self._apply_button_lights(active)
        self.refresh(force=True)

    def _apply_button_lights(self, active=None):
        if not self.bridge:
            return
        try:
            if hasattr(self.bridge, 'set_cockpit_lights'):
                self.bridge.set_cockpit_lights(active=active)
            else:
                self.bridge.wake()
        except Exception as e:
            logging.debug("S88 button light update failed => %s", e)

    def _get_active_context(self):
        title = "-"
        preset = "-"
        proc_name = "-"
        try:
            chain = self.chain_manager.get_active_chain()
            proc = self._get_active_processor(chain)
            if chain:
                try:
                    title = chain.get_title()
                except Exception:
                    title = str(getattr(chain, "chain_id", "active"))
            if proc:
                try:
                    proc_name = proc.get_name()
                except Exception:
                    proc_name = str(getattr(proc, "id", "active"))
                try:
                    preset = proc.get_preset_name()
                except Exception:
                    preset = getattr(proc, "preset_name", None) or getattr(proc, "bank_name", None) or "-"
        except Exception:
            pass
        return title, preset, proc_name

    def _get_active_processor(self, chain=None):
        try:
            if chain is None:
                chain = self.chain_manager.get_active_chain()
            if not chain:
                return None
            proc = getattr(chain, "current_processor", None)
            if proc:
                return proc
            procs = chain.get_processors()
            if procs:
                return procs[0]
        except Exception as e:
            logging.debug("S88 can't get active processor => %s", e)
        return None

    def _get_param_zctrls(self):
        proc = self._get_active_processor()
        if not proc:
            return []
        ctrls = getattr(proc, "controllers_dict", {}) or {}
        zctrls = []
        for zctrl in ctrls.values():
            if getattr(zctrl, "not_on_gui", False):
                continue
            if getattr(zctrl, "readonly", False):
                continue
            if getattr(zctrl, "is_path", False):
                continue
            zctrls.append(zctrl)
        zctrls.sort(key=self._zctrl_sort_key)
        return zctrls

    @staticmethod
    def _natural_key(text):
        return [int(part) if part.isdigit() else part.lower() for part in re.split(r"(\d+)", str(text or ""))]

    def _zctrl_sort_key(self, zctrl):
        name = getattr(zctrl, "short_name", None) or getattr(zctrl, "name", None) or getattr(zctrl, "symbol", "")
        return (-getattr(zctrl, "display_priority", 0), self._natural_key(name))

    def _get_bank_zctrls(self):
        zctrls = self._get_param_zctrls()
        if not zctrls:
            self._param_bank = 0
            return []
        max_bank = max(0, (len(zctrls) - 1) // 8)
        if self._param_bank > max_bank:
            self._param_bank = max_bank
        start = self._param_bank * 8
        return zctrls[start:start + 8]

    def _format_zctrl(self, zctrl):
        name = getattr(zctrl, "short_name", None) or getattr(zctrl, "name", None) or getattr(zctrl, "symbol", "?")
        value = getattr(zctrl, "value", None)
        label = None
        try:
            value2label = getattr(zctrl, "value2label", None)
            if value2label:
                label = value2label.get(str(value))
        except Exception:
            label = None
        if label is None:
            try:
                label = zctrl.get_value2label()
            except Exception:
                label = value
        if isinstance(label, float):
            label = f"{label:.3g}"
        return f"{name}: {label}"

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
            if event.name == "loop":
                self._remember_event(event, "DISPLAY_PAGE")
                self._cycle_display_page(1)
                return
            if event.name == "plugin":
                self._remember_event(event, "BANK_NEXT")
                self._change_param_bank(1)
                return
            if event.name == "midi":
                self._remember_event(event, "BANK_PREV")
                self._change_param_bank(-1)
                return
            cuia = self.CUIA_MAP.get(event.name)
            self._remember_event(event, cuia or "-")
            if cuia:
                self._send_cuia(cuia)
                self.refresh(force=True)
        elif event.kind == "encoder":
            # Coarse v0 mapping: encoder state acts as UI navigation.
            cuia = {
                "encoder_state_0x20": "ARROW_LEFT",
                "encoder_state_0x40": "ARROW_RIGHT",
                "encoder_state_0x80": "ARROW_UP",
                "encoder_state_0xc0": "ARROW_DOWN",
            }.get(event.name)
            self._remember_event(event, cuia or "-")
            if cuia:
                self._send_cuia(cuia)
                self.refresh(force=True)
        elif event.kind == "knob_touch":
            # Capacitive knob touch is intentionally ignored for now.
            # Only knob turns should affect Zynthian state.
            return
        elif event.kind == "knobs8":
            self._remember_event(event, "PARAM_KNOBS")
            self._handle_knobs8(event.value)
        elif event.kind == "knob":
            # Some S88 states only expose a coarse byte-30 stream; treat it as a nudge
            # on the first visible parameter until we map per-knob IDs from longer packets.
            self._remember_event(event, "PARAM_NUDGE")
            self._handle_single_knob(event.value)

    def _change_param_bank(self, delta):
        zctrls = self._get_param_zctrls()
        if not zctrls:
            self._param_bank = 0
        else:
            max_bank = max(0, (len(zctrls) - 1) // 8)
            self._param_bank = max(0, min(max_bank, self._param_bank + delta))
        self.refresh(force=True)

    def _handle_knobs8(self, values):
        if not isinstance(values, list):
            return
        zctrls = self._get_bank_zctrls()
        if not zctrls:
            return
        if self._last_knobs8 is None:
            # First report establishes pickup state; don't jump parameters on attach.
            self._last_knobs8 = list(values)
            return
        for i, raw_val in enumerate(values[:len(zctrls)]):
            try:
                prev = self._last_knobs8[i]
            except Exception:
                prev = raw_val
            if raw_val == prev:
                continue
            self._set_zctrl_from_7bit(zctrls[i], raw_val)
        self._last_knobs8 = list(values)
        self._refresh_after_param_change()

    def _handle_single_knob(self, value):
        zctrls = self._get_bank_zctrls()
        if not zctrls or not isinstance(value, int):
            return
        # The 32-byte fallback stream is not yet per-knob; use relative motion of byte30.
        last = getattr(self, "_last_single_knob", None)
        self._last_single_knob = value
        if last is None or value == last:
            return
        delta = 1 if ((value - last) & 0x7f) < 64 else -1
        try:
            zctrls[0].nudge(delta)
            self._refresh_after_param_change()
        except Exception as e:
            logging.debug("S88 single-knob nudge failed => %s", e)

    def _set_zctrl_from_7bit(self, zctrl, raw_val):
        raw_val = max(0, min(127, int(raw_val)))
        try:
            if getattr(zctrl, "ticks", None):
                ticks = zctrl.ticks
                index = round(raw_val * (len(ticks) - 1) / 127)
                zctrl.set_value(ticks[index])
                return
            value_min = getattr(zctrl, "value_min", 0)
            value_max = getattr(zctrl, "value_max", 127)
            if value_min is None:
                value_min = 0
            if value_max is None:
                value_max = 127
            value = value_min + (raw_val / 127.0) * (value_max - value_min)
            zctrl.set_value(value)
        except Exception as e:
            logging.debug("S88 set param failed for %s => %s", getattr(zctrl, "symbol", zctrl), e)
