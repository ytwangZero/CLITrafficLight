"""
Packages traffic_light_window.py into a standalone CLITrafficLight.app with
py2app -- no Python/pip needed on the machine that runs it.

Must run on macOS (py2app only builds Mac apps), and with a Python that has a
modern Tk (8.6), not the system Python (its Tk 8.5 renders blank/broken UI on
recent macOS):
    brew install python-tk@3.12   # if not already installed
    cd app
    /opt/homebrew/bin/python3.12 -m pip install -r requirements.txt --break-system-packages
    /opt/homebrew/bin/python3.12 setup.py py2app

Output is app/dist/CLITrafficLight.app -- ship that file as-is (zip it first
to preserve permissions in transit).
"""

from setuptools import setup

APP = ["traffic_light_window.py"]
# Bundles both hook scripts into Contents/Resources/hooks/ so the app's
# "Configure Hooks" buttons can find them and wire up ~/.claude/settings.json
# / ~/.codex/hooks.json on any machine, without shipping the hooks/ folder
# separately.
DATA_FILES = [
    ("hooks", ["../hooks/claude_light_hook.py", "../hooks/codex_light_hook.py"]),
]
OPTIONS = {
    "argv_emulation": False,
    "plist": {
        "CFBundleName": "CLITrafficLight",
        "CFBundleDisplayName": "CLI Traffic Light",
        "CFBundleShortVersionString": "1.0.0",
        "CFBundleVersion": "1.0.0",
    },
    "packages": ["serial"],
}

setup(
    app=APP,
    data_files=DATA_FILES,
    options={"py2app": OPTIONS},
    setup_requires=["py2app"],
)
