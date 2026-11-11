"""Platform-aware end-of-job system actions. Never run without explicit UI selection."""
from __future__ import annotations
import ctypes
import os
import platform
import shutil
import subprocess


def available_actions():
    """Return supported action identifiers. Unknown capabilities are not advertised."""
    actions = ['none', 'exit']
    system = platform.system()
    if system == 'Windows':
        # SYSTEM_POWER_CAPABILITIES begins with S1, S2, S3, S4, S5, HiberFilePresent.
        caps = (ctypes.c_ubyte * 256)()
        try:
            ok = ctypes.windll.powrprof.GetPwrCapabilities(ctypes.byref(caps))
            if ok and any(caps[i] for i in (0, 1, 2)):
                actions.append('standby')
            if ok and caps[3] and caps[5]:
                actions.append('hibernate')
        except (AttributeError, OSError):
            pass
        if shutil.which('shutdown') or os.path.exists(os.path.join(os.environ.get('SystemRoot', r'C:\Windows'), 'System32', 'shutdown.exe')):
            actions.append('shutdown')
    elif system == 'Linux':
        if shutil.which('systemctl'):
            try:
                with open('/sys/power/state', encoding='ascii') as f:
                    states = f.read().split()
            except OSError:
                states = []
            if 'mem' in states or 'freeze' in states:
                actions.append('standby')
            if 'disk' in states:
                actions.append('hibernate')
            actions.append('shutdown')
    elif system == 'Darwin':
        if shutil.which('pmset'):
            actions.append('standby')
        if shutil.which('osascript'):
            actions.append('shutdown')
    return actions


def perform_action(action):
    """Execute after UI countdown; raise on failure. No shell=True."""
    if action not in available_actions() or action not in ('standby', 'hibernate', 'shutdown'):
        raise RuntimeError(f'Nicht unterstützte Systemaktion: {action}')
    system = platform.system()
    if system == 'Windows':
        if action == 'shutdown':
            exe = os.path.join(os.environ.get('SystemRoot', r'C:\Windows'), 'System32', 'shutdown.exe')
            if not os.path.isfile(exe):
                exe = 'shutdown'
            subprocess.Popen([exe, '/s', '/t', '0'])
        else:
            result = ctypes.windll.powrprof.SetSuspendState(action == 'hibernate', False, False)
            if not result:
                raise OSError(ctypes.get_last_error(), 'SetSuspendState fehlgeschlagen')
    elif system == 'Linux':
        subprocess.run(['systemctl', {'shutdown':'poweroff','standby':'suspend','hibernate':'hibernate'}[action]], check=True)
    elif system == 'Darwin':
        if action == 'standby':
            subprocess.run(['pmset', 'sleepnow'], check=True)
        elif action == 'shutdown':
            subprocess.run(['osascript', '-e', 'tell application "System Events" to shut down'], check=True)
