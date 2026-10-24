#!/usr/bin/env python3
import sys

from . import auto_load
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent / 'libs'))


bl_info = {
    "name": "WWMI Tools",
    "version": (1, 8, 0, 20),
    "wwmi_version": (1, 1, 0),
    "blender": (4, 5, 0),
    "author": "SpectrumQT, LeoTorreZ, SinsOfSeven, SilentNightSound, DarkStarSword, XQFA",
    "location": "View3D > Sidebar > Tool Tab",
    "description": "Wuthering Waves modding toolkit",
    "category": "Object",
    "tracker_url": "https://github.com/SpectrumQT/WWMI-Tools",
}
auto_load.init()

import bpy
from .addon import settings

# UI translations: Chinese label for the Branch Options drawer
translations_dict = {
    'zh_CN': {
        ('*', 'Branch Options'): '分支选项',
    },
    'zh_HANS': {
        ('*', 'Branch Options'): '分支选项',
    },
}


def trigger_mod_export():
    if bpy.context.scene.wwmi_tools_settings.export_on_reload:
        print('Triggered export on addon reload...')
        bpy.ops.wwmi_tools.export_mod()
    

def register():
    auto_load.register()

    bpy.types.Scene.wwmi_tools_settings = bpy.props.PointerProperty(type=settings.WWMI_Settings)
    
    # prefs = bpy.context.preferences.addons[__package__].preferences
    bpy.app.timers.register(trigger_mod_export, first_interval=0.1)

    bpy.app.translations.register(__package__, translations_dict)


def unregister():
    bpy.app.translations.unregister(__package__)

    auto_load.unregister()

    del bpy.types.Scene.wwmi_tools_settings

    if bpy.app.timers.is_registered(trigger_mod_export):
        bpy.app.timers.unregister(trigger_mod_export)
