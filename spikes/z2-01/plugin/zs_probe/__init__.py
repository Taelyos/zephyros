"""Spike Z2-01 probe plugin: one tool that reports where plugin code runs."""

import json
import os


def register(ctx):
    schema = {
        "name": "zs_probe",
        "description": "Report the process id and parent process id the plugin code runs in.",
        "parameters": {"type": "object", "properties": {}},
    }

    def handler(_args, **_kwargs):
        return json.dumps({"plugin_pid": os.getpid(), "plugin_ppid": os.getppid()})

    ctx.register_tool(name="zs_probe", toolset="zs_probe", schema=schema, handler=handler)
