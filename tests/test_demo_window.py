"""Run the actual JXA source with a strict fake macOS API; no desktop changes."""

import importlib.util
import json
from pathlib import Path
import shutil
import subprocess
import unittest
from unittest.mock import Mock

REPO = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("demo_window_runner_test", REPO / "scripts/demo.py")
demo = importlib.util.module_from_spec(spec)
spec.loader.exec_module(demo)

HARNESS = r"""
const fs = require('fs');
const vm = require('vm');
const input = JSON.parse(process.argv[2]);
const calls = [];
let size = [100, 80], position = [1, 2];
const window = {};
Object.defineProperty(window, 'size', {
    get: () => () => size,
    set: value => { calls.push({size: value}); if (!input.refuseGeometry) size = value; }
});
Object.defineProperty(window, 'position', {
    get: () => () => position,
    set: value => { calls.push({position: value}); if (!input.refuseGeometry) position = value; }
});
const processObject = {windows: {whose: filter => {
    calls.push({windowFilter: filter});
    if (Object.keys(filter).join() !== 'name') throw new Error('invalid window filter');
    return () => filter.name === 'KZW Demo test' ? Array(input.windowCount ?? 1).fill(window) : [];
}}};
const se = new Proxy({
    uiElementsEnabled: () => {
        calls.push('uiElementsEnabled');
        if (input.automationDenied) {
            const error = new Error('raw private Apple event diagnostic');
            error.errorNumber = -1743;
            throw error;
        }
        return input.enabled !== false;
    },
    applicationProcesses: {whose: filter => {
        calls.push({processFilter: filter});
        if (Object.keys(filter).join() !== 'unixId') throw new Error('invalid process filter');
        return () => filter.unixId === 42 ? Array(input.processCount ?? 1).fill(processObject) : [];
    }}
}, {get: (target, key) => {
    if (!(key in target)) throw new Error('invalid System Events property: ' + String(key));
    return target[key];
}});
const screens = input.screens;
const context = {
    ObjC: {import: name => { if (name !== 'AppKit') throw new Error('unexpected framework'); }},
    $: {NSScreen: {screens: {count: screens.length, objectAtIndex: index => screens[index]}},
        NSEvent: {mouseLocation: input.mouse || {x: 100, y: 100}}},
    Application: bundle => {
        calls.push({application: bundle});
        if (bundle !== 'com.apple.systemevents') throw new Error('unexpected application');
        return se;
    }
};
vm.createContext(context);
vm.runInContext(fs.readFileSync(process.argv[1], 'utf8'), context);
let output;
try { output = JSON.parse(context.run(input.args)); }
catch (error) { output = {unhandled: error.message}; }
console.log(JSON.stringify({output, calls}));
"""


def screen(x=0, y=0, width=1920, height=1080, bottom=40, top=30):
    return {"frame": {"origin": {"x": x, "y": y}, "size": {"width": width, "height": height}},
            "visibleFrame": {"origin": {"x": x, "y": y + bottom},
                             "size": {"width": width, "height": height - bottom - top}}}


@unittest.skipUnless(shutil.which("node"), "Node is optional and required for the strict JXA API harness")
class DemoWindowJavaScriptTests(unittest.TestCase):
    def invoke(self, args=None, **overrides):
        fixture = {"screens": [screen()], "args": args or ["screen"], **overrides}
        result = subprocess.run([shutil.which("node"), "-e", HARNESS,
                                 str(REPO / "scripts/demo_window.js"), json.dumps(fixture)],
                                capture_output=True, text=True, timeout=10, check=True)
        return json.loads(result.stdout)

    def place(self, **overrides):
        rect = {"x": 50, "y": 60, "width": 1000, "height": 800}
        return self.invoke(["place", "42", "KZW Demo test", json.dumps(rect)], **overrides)

    def assert_no_mutations(self, result):
        self.assertFalse(any(isinstance(call, dict) and ("size" in call or "position" in call)
                             for call in result["calls"]))

    def test_actual_source_uses_lower_camel_case_ui_elements_property(self):
        result = self.invoke()
        self.assertEqual(result["output"], {"x": 0, "y": 30, "width": 1920, "height": 1010})
        self.assertIn("uiElementsEnabled", result["calls"])
        self.assert_no_mutations(result)

    def test_pointer_selects_secondary_display_with_negative_origin(self):
        result = self.invoke(screens=[screen(), screen(-1600, 100, 1600, 900, 30, 30)],
                             mouse={"x": -100, "y": 500})
        self.assertEqual(result["output"], {"x": -1600, "y": 110, "width": 1600, "height": 840})

    def test_headless_failure_does_not_contact_system_events(self):
        result = self.invoke(screens=[])
        self.assertEqual(result, {"output": {"error": {"code": "no-display"}}, "calls": []})

    def test_accessibility_denial_has_structured_actionable_error(self):
        result = self.invoke(enabled=False)
        self.assertEqual(result["output"], {"error": {"code": "accessibility-required"}})
        self.assert_no_mutations(result)

    def test_automation_denial_does_not_echo_raw_apple_event_error(self):
        result = self.invoke(automationDenied=True)
        self.assertEqual(result["output"], {"error": {"code": "automation-required"}})
        self.assertNotIn("private", json.dumps(result))

    def test_place_is_fenced_by_exact_process_pid_and_window_title(self):
        result = self.place()
        self.assertEqual(result["output"], {"x": 50, "y": 60, "width": 1000, "height": 800})
        self.assertIn({"processFilter": {"unixId": 42}}, result["calls"])
        self.assertIn({"windowFilter": {"name": "KZW Demo test"}}, result["calls"])

    def test_missing_process_and_window_have_distinct_retryable_codes(self):
        for parameter, code in (("processCount", "process-not-ready"), ("windowCount", "window-not-ready")):
            with self.subTest(parameter=parameter):
                result = self.place(**{parameter: 0})
                self.assertEqual(result["output"], {"error": {"code": code}})
                self.assert_no_mutations(result)

    def test_ambiguous_process_and_window_fail_before_mutating(self):
        for parameter, code in (("processCount", "ambiguous-process"), ("windowCount", "ambiguous-window")):
            with self.subTest(parameter=parameter):
                result = self.place(**{parameter: 2})
                self.assertEqual(result["output"], {"error": {"code": code}})
                self.assert_no_mutations(result)

    def test_window_manager_geometry_rejection_is_explicit(self):
        self.assertEqual(self.place(refuseGeometry=True)["output"], {"error": {"code": "geometry-rejected"}})

    def test_unknown_operation_cannot_change_a_window(self):
        result = self.invoke(["anything-else"])
        self.assertEqual(result["output"], {"error": {"code": "unknown-operation"}})
        self.assert_no_mutations(result)


class DemoWindowRunnerTests(unittest.TestCase):
    def runner(self):
        runner = object.__new__(demo.Demo)
        runner.run = Mock()
        runner.mask = Mock(return_value={"pid": 42})
        runner.rc = Mock()
        runner.anchor = 4
        runner.title = "KZW Demo test"
        runner.frame = {"x": 50, "y": 60, "width": 1000, "height": 800}
        return runner

    def test_permission_error_is_explained_directly_not_mislabeled_as_osascript_failure(self):
        runner = self.runner()
        runner.run.return_value = '{"error":{"code":"accessibility-required"}}'
        with self.assertRaisesRegex(demo.WindowError, "System Settings.*Accessibility"):
            runner.window("screen")

    def test_valid_frame_is_returned_unchanged(self):
        runner = self.runner()
        runner.run.return_value = json.dumps(runner.frame)
        self.assertEqual(runner.window("screen"), runner.frame)

    def test_unknown_error_code_is_not_echoed_to_the_recording(self):
        runner = self.runner()
        runner.run.return_value = '{"error":{"code":"private-token"}}'
        with self.assertRaises(demo.WindowError) as raised:
            runner.window("screen")
        self.assertNotIn("private-token", str(raised.exception))

    def test_permission_ambiguity_and_geometry_errors_are_not_retried(self):
        for code in ("accessibility-required", "automation-required", "ambiguous-process", "ambiguous-window", "geometry-rejected"):
            runner = self.runner()
            runner.window = Mock(side_effect=demo.WindowError(code))
            runner.wait = Mock(side_effect=lambda description, predicate, **kwargs: predicate())
            with self.subTest(code=code), self.assertRaises(demo.WindowError):
                runner.show(4)
            runner.window.assert_called_once()

    def test_only_missing_exact_process_or_window_is_retried(self):
        for code in ("process-not-ready", "window-not-ready"):
            runner = self.runner()
            runner.window = Mock(side_effect=[demo.WindowError(code), runner.frame])
            def wait(description, predicate, **kwargs):
                self.assertIsNone(predicate())
                return predicate()
            runner.wait = wait
            with self.subTest(code=code):
                self.assertEqual(runner.show(4), {"pid": 42})
                self.assertEqual(runner.window.call_count, 2)

    def test_unexpected_transport_failure_is_not_silently_retried(self):
        runner = self.runner()
        runner.window = Mock(side_effect=demo.DemoError("osascript failed"))
        runner.wait = Mock(side_effect=lambda description, predicate, **kwargs: predicate())
        with self.assertRaises(demo.DemoError):
            runner.show(4)
        runner.window.assert_called_once()


if __name__ == "__main__":
    unittest.main()
