import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parent.parent
INSTALLER = ROOT / 'scripts/install_iterm.py'
BEGIN = b'# >>> codex-monitor iTerm2 integration >>>'
END = b'# <<< codex-monitor iTerm2 integration <<<'


class ItermInstallation(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='cm-iterm-install-')
        self.root = Path(self.temp.name).resolve()
        self.rc = self.root / '.zshrc'
        self.binary = self.root / 'monitor tools/codex-monitor'
        self.env = dict(os.environ, HOME=str(self.root), CODEX_HOME=str(self.root / '.codex'))

    def tearDown(self):
        self.temp.cleanup()

    def installer(self, *args, check=True):
        return subprocess.run([sys.executable, '-B', str(INSTALLER),
                               '--shell-rc', str(self.rc), '--executable', str(self.binary), *args],
                              capture_output=True, text=True, env=self.env, check=check)

    def test_preview_is_read_only_and_does_not_disclose_existing_rc(self):
        private = b'# DO_NOT_ECHO_SECRET_123\xff\r\nexport BEFORE=1'
        self.rc.write_bytes(private)
        report = self.installer()
        data = json.loads(report.stdout)
        self.assertTrue(data['dry_run'])
        self.assertTrue(data['changed'])
        self.assertIn('launch --codex', data['managed_block'])
        self.assertNotIn('DO_NOT_ECHO_SECRET_123', report.stdout + report.stderr)
        self.assertEqual(self.rc.read_bytes(), private)
        self.assertFalse((self.root / '.codex-monitor-backups').exists())

    def test_install_update_and_uninstall_preserve_other_bytes_and_mode(self):
        original = b'# keep unrelated bytes\xff\r\nexport BEFORE=1'
        self.rc.write_bytes(original)
        self.rc.chmod(0o640)
        first = json.loads(self.installer('--install').stdout)
        installed = self.rc.read_bytes()
        self.assertTrue(installed.startswith(original))
        self.assertEqual(Path(first['backup']).read_bytes(), original)
        self.assertEqual(Path(first['backup']).parent, self.root / '.codex-monitor-backups')
        self.assertEqual(stat.S_IMODE(self.rc.stat().st_mode), 0o640)
        previous_time = self.rc.stat().st_mtime_ns
        again = json.loads(self.installer('--install').stdout)
        self.assertFalse(again['changed'])
        self.assertIsNone(again['backup'])
        self.assertEqual(self.rc.stat().st_mtime_ns, previous_time)

        suffix = b'\n# user-added suffix\r\n'
        self.rc.write_bytes(installed + suffix)
        other_binary = self.root / 'another monitor'
        updated = json.loads(self.installer('--executable', str(other_binary), '--install').stdout)
        self.assertTrue(updated['changed'])
        self.assertTrue(self.rc.read_bytes().startswith(original))
        self.assertTrue(self.rc.read_bytes().endswith(suffix))
        self.assertEqual(self.rc.read_bytes().count(BEGIN), 1)
        self.installer('--uninstall')
        self.assertEqual(self.rc.read_bytes(), original + suffix)
        self.assertEqual(stat.S_IMODE(self.rc.stat().st_mode), 0o640)
        self.assertFalse(json.loads(self.installer('--uninstall').stdout)['changed'])

    def test_absent_rc_and_uninstall_are_safe(self):
        result = json.loads(self.installer('--uninstall').stdout)
        self.assertFalse(result['changed'])
        self.assertFalse(self.rc.exists())
        self.installer('--install')
        self.assertEqual(stat.S_IMODE(self.rc.stat().st_mode), 0o600)
        self.installer('--uninstall')
        self.assertEqual(self.rc.read_bytes(), b'')

    def test_malformed_or_multiple_markers_refuse_without_writing(self):
        examples = [BEGIN + b'\n', END + b'\n', END + b'\n' + BEGIN + b'\n',
                    BEGIN + b'\n' + BEGIN + b'\n' + END + b'\n',
                    b'# malformed ' + BEGIN + b'\n' + END + b'\n']
        for content in examples:
            with self.subTest(content=content):
                self.rc.write_bytes(content)
                report = self.installer('--install', check=False)
                self.assertNotEqual(report.returncode, 0)
                self.assertEqual(self.rc.read_bytes(), content)
        self.assertFalse((self.root / '.codex-monitor-backups').exists())

    def test_symlink_claude_and_skill_backup_paths_are_rejected(self):
        real = self.root / 'original.rc'
        real.write_text('keep original\n')
        self.rc.symlink_to(real)
        self.assertNotEqual(self.installer('--install', check=False).returncode, 0)
        self.assertTrue(self.rc.is_symlink())
        self.assertEqual(real.read_text(), 'keep original\n')
        self.rc.unlink()
        claude = self.root / '.claude/shell.rc'
        result = self.installer('--shell-rc', str(claude), '--install', check=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(claude.exists())
        skill_backup = self.root / '.agents/skills/backups'
        result = self.installer('--backup-dir', str(skill_backup), '--install', check=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(skill_backup.exists())

    def fake_programs(self, with_monitor=True):
        directory = self.root / 'original bin'
        directory.mkdir()
        original = directory / 'codex'
        original.write_text('#!' + sys.executable + '\n'
                            'import json,os,sys\n'
                            'with open(os.environ["CM_ORIGINAL_LOG"],"a") as f:\n'
                            '    f.write(json.dumps(sys.argv[1:])+"\\n")\n')
        original.chmod(0o755)
        if with_monitor:
            self.binary.parent.mkdir()
            self.binary.write_text('#!' + sys.executable + '\n'
                                   'import json,os,sys\n'
                                   'with open(os.environ["CM_MONITOR_LOG"],"a") as f:\n'
                                   '    f.write(json.dumps(sys.argv[1:])+"\\n")\n'
                                   'os.execv(sys.argv[3],[sys.argv[3]]+sys.argv[5:])\n')
            self.binary.chmod(0o755)
        self.env.update(PATH=str(directory) + os.pathsep + os.environ.get('PATH', ''),
                        CM_ORIGINAL_LOG=str(self.root / 'original.log'),
                        CM_MONITOR_LOG=str(self.root / 'monitor.log'))
        self.installer('--install')
        return original

    def zsh(self, script, args=(), interactive=True):
        binary = shutil.which('zsh')
        if not binary:
            self.skipTest('zsh is needed for shell integration checks')
        return subprocess.run([binary, '-f', *(['-i'] if interactive else []), '-c',
                               script, 'cm-shell-test', str(self.rc), *args],
                              env=self.env, capture_output=True, text=True, check=True)

    def test_original_command_and_exact_arguments_are_preserved_without_recursion(self):
        original = self.fake_programs()
        args = ('prompt with spaces', '$(do-not-evaluate)', '--literal', 'a"b', '')
        self.zsh('source "$1"\nshift\ncodex "$@"', args)
        monitor_log = (self.root / 'monitor.log').read_text().splitlines()
        original_log = (self.root / 'original.log').read_text().splitlines()
        self.assertEqual(len(monitor_log), 1)
        self.assertEqual(len(original_log), 1)
        self.assertEqual(json.loads(monitor_log[0]), ['launch', '--codex', str(original), '--', *args])
        self.assertEqual(json.loads(original_log[0]), list(args))

    def test_missing_monitor_executable_falls_back_to_original_codex(self):
        self.fake_programs(with_monitor=False)
        self.zsh('source "$1"\nshift\ncodex "$@"', ('unchanged prompt',))
        self.assertEqual(json.loads((self.root / 'original.log').read_text()), ['unchanged prompt'])
        self.assertFalse((self.root / 'monitor.log').exists())

    def test_existing_function_and_alias_are_not_overridden(self):
        self.fake_programs()
        function = self.zsh('function codex() { print -r -- existing-function; }\nsource "$1"\ncodex')
        self.assertEqual(function.stdout.strip(), 'existing-function')
        alias = self.zsh('alias codex="print -r -- existing-alias"\nsource "$1"\neval "codex"')
        self.assertEqual(alias.stdout.strip(), 'existing-alias')
        self.assertFalse((self.root / 'original.log').exists())
        self.assertFalse((self.root / 'monitor.log').exists())

    def test_noninteractive_shell_does_not_define_a_codex_function(self):
        self.fake_programs()
        self.zsh('source "$1"\nshift\ncodex "$@"', ('original-only',), interactive=False)
        self.assertEqual(json.loads((self.root / 'original.log').read_text()), ['original-only'])
        self.assertFalse((self.root / 'monitor.log').exists())


if __name__ == '__main__':
    unittest.main()
