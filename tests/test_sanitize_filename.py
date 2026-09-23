import builtins
import unittest
from unittest.mock import patch

from moonraker_obico.utils import sanitize_filename


class SanitizeFilenameTests(unittest.TestCase):

    def test_missing_pathvalidate_keeps_regular_filenames_usable(self):
        original_import = builtins.__import__

        def import_without_pathvalidate(name, *args, **kwargs):
            if name == 'pathvalidate':
                raise ModuleNotFoundError("No module named 'pathvalidate'", name='pathvalidate')
            return original_import(name, *args, **kwargs)

        with patch('builtins.__import__', side_effect=import_without_pathvalidate):
            self.assertEqual('part.gcode', sanitize_filename('part.gcode'))
            self.assertEqual('ab.gcode', sanitize_filename('a:b?.gcode'))
            self.assertEqual('CON_.gcode', sanitize_filename('CON.gcode'))
            self.assertEqual('part.gcode', sanitize_filename('.part.gcode'))
            with self.assertRaises(ValueError):
                sanitize_filename('../part.gcode')


if __name__ == '__main__':
    unittest.main()
