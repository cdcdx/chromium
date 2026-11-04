#!/usr/bin/env python3
"""Compatibility launcher for the independently published MiVPN plugin tools."""

import os
from pathlib import Path
import runpy
import sys

workspace = Path(__file__).resolve().parent.parent
project = Path(os.environ.get('MIVPN_PLUGIN_ROOT', str(workspace / 'mivpn-plugin'))).expanduser().resolve()
entry = project / 'tools/sign_mivpn_plugin.py'
if not entry.is_file():
    sys.exit('找不到独立 MiVPN 插件签包工具；请准备 mivpn-plugin 项目或设置 MIVPN_PLUGIN_ROOT。')
runpy.run_path(str(entry), run_name='__main__')
