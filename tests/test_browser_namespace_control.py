"""Control bridge configuration rejects unsafe destinations before launch."""
import importlib.util
import tempfile
import unittest
from pathlib import Path

SPEC = importlib.util.spec_from_file_location('boundary', Path(__file__).parents[1] / 'scripts/browser_namespace.py')
boundary = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(boundary)


class ControlSettingsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.folder = Path(self.temp.name).resolve() / 'control'
        self.folder.mkdir(mode=0o700)
        self.args = ['--remote-debugging-port=43112', '--remote-debugging-address=127.0.0.1']

    def tearDown(self):
        self.temp.cleanup()

    def test_fixed_private_control_endpoint(self):
        self.assertEqual(boundary.control_settings(self.folder, 43112, self.args), self.folder)

    def test_refuses_shared_or_stale_control_directory(self):
        self.folder.chmod(0o755)
        with self.assertRaises(ValueError):
            boundary.control_settings(self.folder, 43112, self.args)
        self.folder.chmod(0o700)
        (self.folder / 'cdp.sock').touch()
        with self.assertRaises(ValueError):
            boundary.control_settings(self.folder, 43112, self.args)

    def test_refuses_symlink(self):
        link = self.folder.parent / 'alias'
        link.symlink_to(self.folder)
        with self.assertRaises(ValueError):
            boundary.control_settings(link, 43112, self.args)

    def test_refuses_wrong_address_port_and_duplicate_override(self):
        for args in ([], ['--remote-debugging-port=43112', '--remote-debugging-address=0.0.0.0'],
                     self.args + ['--remote-debugging-port=99'], self.args + ['--remote-debugging-address', '0.0.0.0']):
            with self.subTest(args=args), self.assertRaises(ValueError):
                boundary.control_settings(self.folder, 43112, args)
        for port in (0, 80, 65536, 'not-a-port'):
            with self.subTest(port=port), self.assertRaises(ValueError):
                boundary.control_settings(self.folder, port, self.args)


if __name__ == '__main__':
    unittest.main()
