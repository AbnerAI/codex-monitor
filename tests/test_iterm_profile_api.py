import asyncio
import io
import json
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from codex_monitor.resources import iterm_profile_api as bridge


SESSION = "11111111-2222-4333-8444-555555555555"
LAYOUT = {"components": [{"class": "existing", "configuration": {
    "knobs": {"priority": 5, "enabled": True, "ratio": 0.25, "text": "雪", "empty": None}}}]}


def response(field, **values):
    return SimpleNamespace(**{field: SimpleNamespace(status=0, **values)})


def fake_api(layout=LAYOUT, enabled=False):
    connection = SimpleNamespace(websocket=SimpleNamespace(close=AsyncMock()))
    properties = [SimpleNamespace(key=bridge.LAYOUT, json_value=json.dumps(layout)),
                  SimpleNamespace(key=bridge.ENABLED, json_value=json.dumps(enabled))]
    api = SimpleNamespace(
        Connection=SimpleNamespace(async_create=AsyncMock(return_value=connection)),
        rpc=SimpleNamespace(
            async_get_profile=AsyncMock(return_value=response("get_profile_property_response", properties=properties)),
            async_set_profile_properties_json=AsyncMock(return_value=response("set_profile_property_response")),
            async_variable=AsyncMock(return_value=response("variable_response"))),
        api_pb2=SimpleNamespace(**{name: SimpleNamespace(OK=0) for name in (
            "GetProfilePropertyResponse", "SetProfilePropertyResponse", "VariableResponse")}))
    return api, connection


class ItermProfileApi(unittest.TestCase):
    def test_get_preserves_nested_types_and_reads_only_exact_session_keys(self):
        api, connection = fake_api()
        result = bridge.handle_request({"action": "get", "session_id": SESSION}, api)
        self.assertEqual(result, {"ok": True, "result": {"layout": LAYOUT, "enabled": False}})
        api.rpc.async_get_profile.assert_awaited_once_with(
            connection, session=SESSION, keys=bridge.PROFILE_KEYS)
        api.rpc.async_set_profile_properties_json.assert_not_awaited()
        api.rpc.async_variable.assert_not_awaited()
        connection.websocket.close.assert_awaited_once()

    def test_apply_targets_session_and_transmits_typed_json_then_reads_back(self):
        api, connection = fake_api(enabled=True)
        result = bridge.handle_request({"action": "apply", "session_id": SESSION,
                                       "properties": {bridge.LAYOUT: LAYOUT, bridge.ENABLED: True}}, api)
        self.assertTrue(result["ok"])
        call = api.rpc.async_set_profile_properties_json.await_args
        self.assertEqual(call.args, (connection,))
        self.assertEqual(set(call.kwargs), {"session_id", "assignments"})
        self.assertEqual(call.kwargs["session_id"], SESSION)
        self.assertEqual({key: json.loads(value) for key, value in call.kwargs["assignments"]},
                         {bridge.LAYOUT: LAYOUT, bridge.ENABLED: True})
        self.assertEqual(result["result"]["enabled"], True)

    def test_clear_changes_only_monitor_variable_without_profile_access(self):
        api, connection = fake_api()
        result = bridge.handle_request({"action": "clear", "session_id": SESSION}, api)
        self.assertEqual(result, {"ok": True, "result": {"cleared": True}})
        api.rpc.async_variable.assert_awaited_once_with(
            connection, session_id=SESSION, sets=[("user.codexMonitor", '""')])
        api.rpc.async_get_profile.assert_not_awaited()
        api.rpc.async_set_profile_properties_json.assert_not_awaited()

    def test_identity_reads_only_explicit_session_native_metadata(self):
        api, connection = fake_api()
        native = 'codex | 11111111-2222-4333-8444-55555... | codex | Task'
        api.rpc.async_variable.return_value = response('variable_response', values=[json.dumps(native)])
        result = bridge.handle_request({'action': 'identity', 'session_id': SESSION}, api)
        self.assertEqual(result, {'ok': True, 'result': {'native_title': native}})
        api.rpc.async_variable.assert_awaited_once_with(
            connection, session_id=SESSION, gets=['session.autoNameFormat'])
        api.rpc.async_get_profile.assert_not_awaited()
        api.rpc.async_set_profile_properties_json.assert_not_awaited()

    def test_identity_rejects_unexpected_metadata_without_writing(self):
        for values in ([], ['{}'], ['1'], ['"one"', '"two"']):
            api, connection = fake_api()
            api.rpc.async_variable.return_value = response('variable_response', values=values)
            self.assertFalse(bridge.handle_request({'action': 'identity', 'session_id': SESSION}, api)['ok'])
            api.rpc.async_set_profile_properties_json.assert_not_awaited()

    def test_invalid_targets_properties_and_types_never_connect(self):
        api, _ = fake_api()
        invalid = [
            {"action": "get", "session_id": value}
            for value in (None, "", "active", "all", "w0t0p0:" + SESSION, SESSION + "\n")]
        invalid += [{"action": "apply", "session_id": SESSION, "properties": value}
                    for value in ({}, {"Name": "changed"}, {bridge.ENABLED: 1},
                                  {bridge.ENABLED: "true"}, {bridge.LAYOUT: []},
                                  {bridge.LAYOUT: {"width": float("nan")}})]
        invalid += [{"action": "get", "session_id": SESSION, "guid": SESSION},
                    {"action": "clear", "session_id": SESSION, "properties": {bridge.ENABLED: False}}]
        for request in invalid:
            with self.subTest(request=request):
                self.assertFalse(bridge.handle_request(request, api)["ok"])
        api.Connection.async_create.assert_not_awaited()

    def test_null_layout_is_supported_without_coercing_boolean(self):
        api, _ = fake_api(layout=None, enabled=False)
        self.assertEqual(bridge.handle_request({"action": "get", "session_id": SESSION}, api)["result"],
                         {"layout": None, "enabled": False})

    def test_native_checkbox_zero_and_one_normalize_without_changing_layout(self):
        for value, expected in ((0, False), (1, True)):
            with self.subTest(value=value):
                api, _ = fake_api(enabled=value)
                result = bridge.handle_request({"action": "get", "session_id": SESSION}, api)
                self.assertTrue(result["ok"])
                self.assertIs(result["result"]["enabled"], expected)
                self.assertEqual(result["result"]["layout"], LAYOUT)
                knobs = result["result"]["layout"]["components"][0]["configuration"]["knobs"]
                self.assertIs(type(knobs["priority"]), int)
                self.assertIs(type(knobs["enabled"]), bool)
                self.assertIs(type(knobs["ratio"]), float)

    def test_other_checkbox_readback_values_fail_closed(self):
        for value in (2, -1, 1.0, "1", "true", None):
            with self.subTest(value=value):
                api, connection = fake_api(enabled=value)
                self.assertFalse(bridge.handle_request({"action": "get", "session_id": SESSION}, api)["ok"])
                api.rpc.async_set_profile_properties_json.assert_not_awaited()
                connection.websocket.close.assert_awaited_once()

    def test_rejected_apply_does_not_proceed_to_readback_and_closes(self):
        api, connection = fake_api()
        api.rpc.async_set_profile_properties_json.return_value.set_profile_property_response.status = 1
        result = bridge.handle_request({"action": "apply", "session_id": SESSION,
                                       "properties": {bridge.ENABLED: True}}, api)
        self.assertFalse(result["ok"])
        api.rpc.async_get_profile.assert_not_awaited()
        connection.websocket.close.assert_awaited_once()

    def test_api_disabled_diagnostics_and_auth_details_do_not_escape(self):
        api, _ = fake_api()

        async def disabled():
            print("private-cookie-value")
            raise ConnectionRefusedError("private-cookie-value")

        api.Connection.async_create.side_effect = disabled
        stdout = io.StringIO()
        with patch("sys.stdout", stdout):
            result = bridge.handle_request({"action": "get", "session_id": SESSION}, api)
        self.assertFalse(result["ok"])
        self.assertEqual(stdout.getvalue(), "")
        self.assertNotIn("private-cookie-value", json.dumps(result))

    def test_timeout_closes_established_connection(self):
        api, connection = fake_api()

        async def stalled(*args, **kwargs):
            await asyncio.sleep(10)

        api.rpc.async_get_profile.side_effect = stalled
        result = bridge.handle_request({"action": "get", "session_id": SESSION}, api, timeout=0.01)
        self.assertEqual(result, {"ok": False, "error": "iTerm2 API request timed out"})
        connection.websocket.close.assert_awaited_once()

    def test_stdout_is_one_json_object_for_malformed_or_missing_sdk(self):
        stdout = io.StringIO()
        bridge.main(io.StringIO("{"), stdout)
        self.assertFalse(json.loads(stdout.getvalue())["ok"])
        self.assertEqual(len(stdout.getvalue().splitlines()), 1)
        with patch.object(bridge.importlib, "import_module", side_effect=ModuleNotFoundError()):
            result = bridge.handle_request({"action": "get", "session_id": SESSION})
        self.assertEqual(result, {"ok": False, "error": "Optional iterm2 Python package is unavailable"})
