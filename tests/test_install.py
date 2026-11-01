import json
import os
from pathlib import Path
import runpy
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from codex_monitor import __version__

ROOT = Path(__file__).resolve().parent.parent


class Installation(unittest.TestCase):
    def test_offline_install_and_skill_wrapper_work_without_path_entry(self):
        with tempfile.TemporaryDirectory(prefix='cm-install-') as temp:
            root = Path(temp)
            skill_root = root / 'skills'
            skill = skill_root / 'codex-monitor'
            command = [sys.executable, '-B', str(ROOT / 'scripts/install.py'),
                       '--prefix', str(root / 'service'), '--bin-dir', str(root / 'bin'),
                       '--skill-dir', str(skill)]
            result = subprocess.run(command, check=True, capture_output=True, text=True)
            report = json.loads(result.stdout)
            self.assertFalse(report['codex_config_changed'])
            self.assertFalse(report['service_started'])
            wrapper = skill / 'scripts/monitor.py'
            completed = subprocess.run([sys.executable, '-B', str(wrapper), '--version'],
                                       check=True, capture_output=True, text=True)
            self.assertEqual(completed.stdout.strip(), __version__)
            (skill / 'old-only.txt').write_text('preserve this previous installation\n')
            (skill / 'SKILL.md').write_text('previous skill instructions\n')
            again = subprocess.run(command, check=True, capture_output=True, text=True)
            backups = [Path(item) for item in json.loads(again.stdout)['backups']]
            self.assertEqual(len(backups), 3)
            saved_skill = next(item for item in backups if (item / 'old-only.txt').is_file())
            self.assertEqual(saved_skill.parent, root / 'service/backups/skills')
            self.assertNotIn(skill_root, saved_skill.parents)
            self.assertEqual((saved_skill / 'SKILL.md').read_text(), 'previous skill instructions\n')
            self.assertEqual((saved_skill / 'old-only.txt').read_text(),
                             'preserve this previous installation\n')
            self.assertEqual(list(skill_root.rglob('SKILL.md')), [skill / 'SKILL.md'])
            self.assertFalse((skill / 'old-only.txt').exists())

    def test_partial_copy_failure_restores_the_complete_original_skill(self):
        install = runpy.run_path(str(ROOT / 'scripts/install.py'))['install']
        with tempfile.TemporaryDirectory(prefix='cm-install-rollback-') as temp:
            root = Path(temp)
            skill = root / 'skills/codex-monitor'
            skill.mkdir(parents=True)
            (skill / 'SKILL.md').write_text('original skill\n')
            (skill / 'private-note.txt').write_text('original note\n')
            backup_dir = root / 'service/backups/skills'

            def incomplete_copy(source, target, **kwargs):
                target.mkdir()
                (target / 'SKILL.md').write_text('incomplete replacement\n')
                raise OSError('copy interrupted')

            with patch('shutil.copytree', side_effect=incomplete_copy):
                with self.assertRaisesRegex(OSError, 'copy interrupted'):
                    install(root / 'source', skill, backup_dir=backup_dir)
            self.assertEqual((skill / 'SKILL.md').read_text(), 'original skill\n')
            self.assertEqual((skill / 'private-note.txt').read_text(), 'original note\n')
            self.assertEqual(list(backup_dir.iterdir()), [])
            self.assertEqual(list(skill.parent.rglob('SKILL.md')), [skill / 'SKILL.md'])

    def test_external_backup_does_not_allow_replacing_a_symlink(self):
        install = runpy.run_path(str(ROOT / 'scripts/install.py'))['install']
        with tempfile.TemporaryDirectory(prefix='cm-install-symlink-') as temp:
            root = Path(temp)
            original = root / 'original'
            original.mkdir()
            (original / 'SKILL.md').write_text('keep original\n')
            skill = root / 'skill'
            skill.symlink_to(original, target_is_directory=True)
            backup_dir = root / 'service/backups/skills'
            with self.assertRaisesRegex(ValueError, 'symlink'):
                install(root / 'source', skill, backup_dir=backup_dir)
            self.assertTrue(skill.is_symlink())
            self.assertEqual((original / 'SKILL.md').read_text(), 'keep original\n')
            self.assertFalse(backup_dir.exists())

    def test_installer_rejects_a_claude_destination_before_writing(self):
        with tempfile.TemporaryDirectory(prefix='cm-install-') as temp:
            target = Path(temp) / '.claude/skills/codex-monitor'
            result = subprocess.run([sys.executable, '-B', str(ROOT / 'scripts/install.py'),
                                     '--prefix', str(target)], capture_output=True, text=True)
            self.assertNotEqual(result.returncode, 0)
            self.assertFalse(target.exists())
