"""Launch the fixed offline kitten editor. Importing creates no process."""
from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys

from activity_core import no_links


def _app_root():
    if getattr(sys,'frozen',False):return Path(sys.executable).absolute().parent
    folder=Path(__file__).absolute().parent
    return folder.parent if folder.name=='src' and (folder.parent/'assets').is_dir() else folder


class TricksLaunchError(RuntimeError):
    def __init__(self):super().__init__('tricks_editor_unavailable')


def launch(root=None,*,popen=None):
    """No caller-supplied command, URL, shell or browser profile is accepted."""
    folder=Path(root if root is not None else _app_root()).absolute()
    try:
        no_links(folder)
        if getattr(sys,'frozen',False):
            helper=folder/'ProstranstvoCore.exe';no_links(helper,True)
            if not helper.is_file():raise ValueError()
            command=[str(helper),'tricks','--open']
        else:
            config=folder/'python-path.txt';no_links(config,True)
            if config.stat().st_size>2048:raise ValueError()
            helper=Path(config.read_text(encoding='utf-8-sig').strip())
            if not helper.is_absolute() or helper.name.lower() not in {'python.exe','pythonw.exe'}:raise ValueError()
            no_links(helper,True)
            script=folder/'pet_tricks_server.py';no_links(script,True)
            if not helper.is_file() or not script.is_file():raise ValueError()
            command=[str(helper),'-B',str(script),'--open']
        spawn=popen if popen is not None else subprocess.Popen
        return spawn(command,cwd=str(folder),stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,
                     stderr=subprocess.DEVNULL,shell=False,creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
    except (OSError,ValueError,TypeError):raise TricksLaunchError() from None
