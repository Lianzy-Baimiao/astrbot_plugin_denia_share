"""Isolated real config boundary for test_config_ui.cjs; never reads bot settings."""
import json
import sys
from unittest.mock import patch

from test_share_output import ROOT, api, config


class StoredConfig(dict):
    def save_config(self):
        # Exercise persistence serialization without touching a running bot.
        self.serialized = json.dumps(self, ensure_ascii=False)


request = json.load(sys.stdin)
stored = StoredConfig(request.get("state", {}))
config.migrate_grouped_config(stored)
parser = config.ParserConfig(stored, ROOT, ROOT)
with patch.dict(sys.modules, {"astrbot.api": api}):
    changed, errors = parser.apply_updates(request.get("values", {}))
print(json.dumps({
    **config.config_meta_payload(), "values": parser.current_values(),
    "changed": changed, "errors": errors, "flags": parser.send_mode_flags,
    "state": json.loads(stored.serialized) if hasattr(stored, "serialized") else stored,
}, ensure_ascii=False))
