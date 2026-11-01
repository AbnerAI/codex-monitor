import importlib.util
from pathlib import Path
import unittest

path = Path(__file__).resolve().parent.parent / 'scripts/session_check.py'
spec = importlib.util.spec_from_file_location('session_check', path)
session_check = importlib.util.module_from_spec(spec)
spec.loader.exec_module(session_check)


class SessionEvidence(unittest.TestCase):
    def test_canonical_ack_requires_the_accepted_turn_and_actual_agent_item(self):
        entries = [
            {'turnId': 'previous-turn', 'item': {'id': 'old', 'type': 'agentMessage', 'text': 'ACK'}},
            {'turnId': 'accepted-turn', 'item': {'id': 'tool', 'type': 'functionCallOutput', 'output': 'ACK'}},
            {'turnId': 'accepted-turn', 'item': {'id': 'actual', 'type': 'agentMessage', 'text': 'ACK'}},
        ]
        found = session_check.find_agent_ack(entries, 'accepted-turn', 'ACK')
        self.assertEqual(found['item']['id'], 'actual')

    def test_assistant_shaped_source_dictionary_cannot_pass_canonical_check(self):
        entries = [{'turnId': 'accepted-turn', 'item': {'type': 'unknownSource',
                    'payload': {'role': 'assistant', 'content': 'ACK'}}}]
        self.assertIsNone(session_check.find_agent_ack(entries, 'accepted-turn', 'ACK'))

    def test_only_real_agent_text_counts_as_an_acknowledgement(self):
        items = [
            {'type': 'functionCallOutput', 'output': {'role': 'assistant', 'content': 'fake'}},
            {'type': 'userMessage', 'content': [{'type': 'text', 'text': 'fake'}]},
            {'type': 'commandExecution', 'aggregatedOutput': {'type': 'agentMessage', 'text': 'fake'}},
            {'type': 'agentMessage', 'text': 'MONITOR_ACK:real'},
        ]
        self.assertEqual(list(session_check.agent_texts(items)), ['MONITOR_ACK:real'])

    def test_wrapped_assistant_output_text_is_supported_without_tool_arguments(self):
        items = [{'item': {'role': 'assistant', 'content': [
            {'type': 'output_text', 'text': 'MONITOR_ACK:real'},
            {'type': 'function_call', 'arguments': 'fake'}]}}]
        self.assertEqual(list(session_check.agent_texts(items)), ['MONITOR_ACK:real'])
