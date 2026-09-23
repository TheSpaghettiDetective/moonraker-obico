from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import patch

from moonraker_obico.moonraker_conn import MoonrakerConn


class IncludeCfgsTests(TestCase):

    def test_setup_works_without_external_script_and_is_idempotent(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            module_file = root / 'moonraker_obico' / 'moonraker_conn.py'
            module_file.parent.mkdir()
            module_file.touch()
            macro_cfg = root / 'include_cfgs' / 'moonraker_obico_macros.cfg'
            macro_cfg.parent.mkdir()
            macro_cfg.write_text('[gcode_macro TEST]\n')
            printer_cfg = root / 'printer data' / 'printer.cfg'
            printer_cfg.parent.mkdir()
            printer_cfg.write_text('[printer]\n#*# -- SAVE_CONFIG --\n#*# saved value\n')
            conn = SimpleNamespace(api_get=lambda *args, **kwargs: {'config_file': str(printer_cfg)})

            self.assertFalse((root / 'scripts' / 'ensure_include_cfgs.sh').exists())
            with patch('moonraker_obico.moonraker_conn.__file__', str(module_file)):
                MoonrakerConn._setup_include_cfgs(conn)
                MoonrakerConn._setup_include_cfgs(conn)

            self.assertEqual(macro_cfg.resolve(), (printer_cfg.parent / macro_cfg.name).resolve())
            self.assertEqual(
                '[printer]\n[include moonraker_obico_macros.cfg]\n#*# -- SAVE_CONFIG --\n#*# saved value\n',
                printer_cfg.read_text(),
            )


if __name__ == '__main__':
    import unittest
    unittest.main()
