"""install.sh, run in a throwaway HOME with fake systemctl and sudo: the real system is never touched."""
import json
import os
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SUNSHINE_UNIT = "app-dev.lizardbyte.app.Sunshine.service"

FAKE_SYSTEMCTL = """#!/bin/bash
echo "systemctl $*" >> "$FAKE_LOG"
case "$*" in
  *is-active*) exit 3 ;;
  *cat*) [ -z "$FAKE_NO_SUNSHINE" ] ;;
  *) exit 0 ;;
esac
"""
FAKE_SUDO = """#!/bin/bash
echo "sudo $*" >> "$FAKE_LOG"
[ -z "$FAKE_NO_HELPER" ] && echo "status=disconnected enabled=disabled"
"""


def conf_settings(path):
    settings = {}
    for line in path.read_text().splitlines():
        key, sep, value = line.partition("=")
        if sep:
            settings[key.strip()] = value.strip()
    return settings


class InstallTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.home = Path(tmp.name) / "home"
        self.home.mkdir()
        fakes = Path(tmp.name) / "fakes"
        fakes.mkdir()
        for name, text in (("systemctl", FAKE_SYSTEMCTL), ("sudo", FAKE_SUDO)):
            (fakes / name).write_text(text)
            (fakes / name).chmod(0o755)
        self.log = Path(tmp.name) / "calls.log"
        self.log.touch()
        self.env = {"HOME": str(self.home), "PATH": f"{fakes}:/usr/bin:/bin", "FAKE_LOG": str(self.log),
                    "XDG_RUNTIME_DIR": tmp.name, "SUNSHINE_WEB_CREDENTIALS": "admin:s3cret",
                    "ADAPTER_NAME": "/dev/dri/by-path/pci-0000:00:02.0-render"}
        self.dest = self.home / ".local/share/sunshine-ipad-virtual"
        self.conf = self.home / ".config/sunshine/sunshine.conf"

    def run_install(self, *args, **env):
        return subprocess.run(["bash", str(REPO / "install.sh"), *args], env={**self.env, **env},
                              capture_output=True, text=True, stdin=subprocess.DEVNULL, timeout=60)

    def prep_cmds(self):
        return json.loads(conf_settings(self.conf)["global_prep_cmd"])

    def test_fresh_install_places_files_and_enables_the_service(self):
        result = self.run_install()
        self.assertEqual(result.returncode, 0, result.stderr)
        for name in ("phantom/controller.py", "phantom/display.py", "phantom/contact.py",
                     "phantom/sunshine_api.py", "bin/phantomctl", "bin/sunshine-ipad-mode"):
            self.assertTrue((self.dest / name).is_file(), name)
        self.assertEqual(list(self.dest.glob("phantom/test_*.py")), [])
        self.assertTrue(os.access(self.dest / "bin/phantomctl", os.X_OK))
        self.assertEqual((self.home / ".local/bin/sunshine-ipad-mode").resolve(),
                         self.dest / "bin/sunshine-ipad-mode")
        units = self.home / ".config/systemd/user"
        self.assertTrue((units / "sunshine-phantom.service").is_file())
        self.assertTrue((units / f"{SUNSHINE_UNIT}.d/50-phantom-wake.conf").is_file())
        calls = self.log.read_text()
        self.assertIn("systemctl --user daemon-reload", calls)
        self.assertIn("systemctl --user enable sunshine-phantom.service", calls)
        settings = conf_settings(self.conf)
        self.assertEqual((settings["capture"], settings["encoder"], settings["output_name"]),
                         ("kms", "vaapi", "HDMI-A-1"))
        self.assertEqual(settings["adapter_name"], "/dev/dri/by-path/pci-0000:00:02.0-render")
        phantomctl = self.dest / "bin/phantomctl"
        self.assertEqual(self.prep_cmds(), [{"do": f"{phantomctl} session-start",
                                             "undo": f"{phantomctl} session-end", "elevated": False}])
        credentials = self.home / ".config/sunshine-ipad-virtual/credentials"
        self.assertEqual(credentials.read_text(), "admin:s3cret\n")
        self.assertEqual(stat.S_IMODE(credentials.stat().st_mode), 0o600)

    def test_other_prep_commands_are_kept_and_ours_is_not_duplicated(self):
        # global_prep_cmd may hold other tools' commands; an old entry of ours (any path) is replaced.
        self.conf.parent.mkdir(parents=True)
        other = {"do": "/usr/bin/other-tool start", "undo": "/usr/bin/other-tool stop", "elevated": False}
        old = {"do": "/opt/old/bin/phantomctl session-start", "undo": "/opt/old/bin/phantomctl session-end",
               "elevated": False}
        self.conf.write_text(f"locale = zh\nglobal_prep_cmd = {json.dumps([other, old])}\n")
        self.assertEqual(self.run_install().returncode, 0)
        self.assertEqual(self.run_install().returncode, 0)
        cmds = self.prep_cmds()
        self.assertEqual(cmds[0], other)
        self.assertEqual(len(cmds), 2)
        self.assertIn(str(self.dest / "bin/phantomctl"), cmds[1]["do"])
        self.assertEqual(conf_settings(self.conf)["locale"], "zh")

    def test_sunshine_conf_is_backed_up_before_it_changes(self):
        self.conf.parent.mkdir(parents=True)
        self.conf.write_text("locale = zh\n")
        self.assertEqual(self.run_install().returncode, 0)
        backups = list(self.conf.parent.glob("sunshine.conf.bak-*"))
        self.assertEqual(len(backups), 1)
        self.assertEqual(backups[0].read_text(), "locale = zh\n")

    def test_existing_credentials_are_left_alone(self):
        credentials = self.home / ".config/sunshine-ipad-virtual/credentials"
        credentials.parent.mkdir(parents=True)
        credentials.write_text("mine:kept\n")
        self.assertEqual(self.run_install().returncode, 0)
        self.assertEqual(credentials.read_text(), "mine:kept\n")

    def test_missing_prerequisites_stop_before_any_change(self):
        for env in ({"FAKE_NO_HELPER": "1"}, {"FAKE_NO_SUNSHINE": "1"}, {"SUNSHINE_WEB_CREDENTIALS": ""}):
            result = self.run_install(**env)
            self.assertNotEqual(result.returncode, 0, env)
            self.assertTrue(result.stderr.strip(), env)
            self.assertEqual(list(self.home.iterdir()), [], env)

    def test_unreadable_prep_command_list_is_left_untouched(self):
        self.conf.parent.mkdir(parents=True)
        self.conf.write_text("global_prep_cmd = [{broken\n")
        result = self.run_install()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("global_prep_cmd", result.stderr)
        self.assertEqual(self.conf.read_text(), "global_prep_cmd = [{broken\n")
        self.assertFalse(self.dest.exists())

    def test_running_from_the_install_path_keeps_the_code_on_uninstall(self):
        # A checkout living at the install path is the user's source, not an installed copy.
        self.dest.parent.mkdir(parents=True)
        subprocess.run(["cp", "-r", str(REPO), str(self.dest)], check=True)
        subprocess.run(["rm", "-rf", str(self.dest / ".git")], check=True)
        for args in ((), ("--uninstall",)):
            result = subprocess.run(["bash", str(self.dest / "install.sh"), *args], env=self.env,
                                    capture_output=True, text=True, stdin=subprocess.DEVNULL, timeout=60)
            self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue((self.dest / "phantom/controller.py").is_file())
        self.assertTrue((self.dest / "phantom/test_install.py").is_file())

    def test_uninstall_removes_only_what_install_added(self):
        self.conf.parent.mkdir(parents=True)
        other = {"do": "/usr/bin/other-tool start", "undo": "/usr/bin/other-tool stop", "elevated": False}
        self.conf.write_text(f"global_prep_cmd = {json.dumps([other])}\n")
        self.assertEqual(self.run_install().returncode, 0)
        (self.dest / "notes.txt").write_text("not ours")  # e.g. a git checkout living at the install path
        result = self.run_install("--uninstall")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("systemctl --user disable --now sunshine-phantom.service", self.log.read_text())
        self.assertFalse((self.dest / "phantom/controller.py").exists())
        self.assertFalse((self.dest / "bin/phantomctl").exists())
        self.assertTrue((self.dest / "notes.txt").exists())
        self.assertFalse((self.home / ".local/bin/sunshine-ipad-mode").exists())
        self.assertFalse((self.home / ".config/systemd/user/sunshine-phantom.service").exists())
        self.assertFalse((self.home / f".config/systemd/user/{SUNSHINE_UNIT}.d/50-phantom-wake.conf").exists())
        self.assertFalse((self.home / ".config/sunshine-ipad-virtual").exists())
        settings = conf_settings(self.conf)
        self.assertEqual(json.loads(settings["global_prep_cmd"]), [other])
        self.assertNotIn("output_name", settings)
        self.assertEqual(settings["capture"], "kms")  # a valid Sunshine setting on its own: left in place


if __name__ == "__main__":
    unittest.main()
