#!/usr/bin/python3
# ==============================================================================
# ZYNTHIAN DMX ENGINE WRAPPER
# Evolux SOG Alien 500mW Laser Controller
# By LoveSlap Recordings
# ==============================================================================

import sys
import os
import json
import time

# Add parent dir to path for zyngine imports
sys.path.insert(0, '/zynthian/zynthian-ui')

from liblo import Server

# Import Zynthian engine base class
sys.path.insert(0, '/zynthian/zynthian-config/zynconfig')

# Engine constants
ENGINE_TYPE = "DMX"
ENGINE_NAME = "DMX Laser"
ENGINE_ID = 8  # Special engine type

# Paths
DMX_ENGINE_PATH = "/zynthian/zynthian-sw/zynthian_engine_dmx"
DMX_DAEMON = f"{DMX_ENGINE_PATH}/zynthian_engine_dmx.py"
DMX_LOG = "/tmp/dmx_engine.log"

# OSC ports
OSC_PORT = 9802  # Control in
OSC_STATUS_PORT = 9803  # Status out

# Controller definitions
CONTROLLERS = {
    1: {"name": "Color", "type": "int", "min": 0, "max": 255, "default": 128, "step": 1},
    2: {"name": "Pattern", "type": "int", "min": 0, "max": 255, "default": 64, "step": 1},
    3: {"name": "Strobe", "type": "int", "min": 0, "max": 255, "default": 0, "step": 1},
    4: {"name": "Master", "type": "int", "min": 0, "max": 255, "default": 160, "step": 1},  # DMX mode
}

class ZynthianEngineDMX:
    def __init__(self):
        self.process = None
        self.active = False
        
    def get_controllers(self):
        return CONTROLLERS

    def engine_name(self):
        return ENGINE_NAME

    def start(self):
        """Start the DMX daemon"""
        import subprocess
        
        try:
            self.process = subprocess.Popen(
                ["/usr/bin/python3", DMX_DAEMON],
                cwd=DMX_ENGINE_PATH,
                stdout=open(DMX_LOG, "a"),
                stderr=subprocess.STDOUT,
                env=os.environ.copy()
            )
            
            # Wait for startup
            time.sleep(2)
            
            if self.process.poll() is None:
                self.active = True
                return True
            else:
                return False
                
        except Exception as e:
            self.active = False
            return False

    def stop(self):
        """Stop the DMX daemon"""
        if self.process:
            try:
                self.process.terminate()
                time.sleep(0.5)
                if self.process.poll() is None:
                    self.process.kill()
            except:
                pass
        self.active = False

    def toggle(self, active):
        """Toggle engine"""
        if active:
            self.start()
        else:
            self.stop()

    def is_active(self):
        return self.active

    def send_osc(self, path, *args):
        """Send OSC message to daemon"""
        try:
            from liblo import send
            dest = Server(OSC_STATUS_PORT)
            send(dest, path, *args)
        except:
            pass

    def receive_osc(self, ip, path, *args):
        """Handle incoming OSC from daemon"""
        print(f"OSC: {path} -> {args}")


if __name__ == "__main__":
    e = ZynthianEngineDMX()
    print("DMX Engine Wrapper Initialized")
    print(f"Engine name: {e.engine_name()}")
    print(f"Controllers: {list(CONTROLLERS.keys())}")
