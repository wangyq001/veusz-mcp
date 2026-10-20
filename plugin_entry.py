"""Loaded by Veusz --veusz-plugin; Veusz does not provide __file__ here."""
import os
import sys

sys.path.insert(0, os.environ["VEUSZ_BRIDGE_ROOT"])
try:
    from veusz_bridge.plugin import install
    install()
    if os.environ.get("VEUSZ_BRIDGE_TEST_MODE") == "1":
        from veusz import setting, qtall
        app = qtall.QApplication.instance()
        app._codex_veusz_bridge.test_preferences = {}
        for key, value in (("ask_tutorial", True), ("vercheck_disabled", True), ("feedback_disabled", True)):
            app._codex_veusz_bridge.test_preferences[key] = setting.settingdb[key]
            setting.settingdb[key] = value
except Exception:
    import traceback
    with open(os.path.join(os.path.dirname(os.environ["VEUSZ_BRIDGE_SESSION_FILE"]),
                           "plugin_error.txt"), "w", encoding="utf-8") as error_file:
        error_file.write(traceback.format_exc())
    raise
