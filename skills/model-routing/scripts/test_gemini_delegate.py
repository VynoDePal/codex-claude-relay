import unittest
import json
import tempfile
from pathlib import Path
from unittest.mock import patch

from gemini_delegate import build_payload, verified_result, delegate


class GeminiDelegationTests(unittest.TestCase):
    def response(self):
        return {'status': 'completed', 'model': 'gemini-4-argon',
                'metadata': {'antigravity_model': 'gemini-4-argon-high'},
                'output': [{'type': 'message', 'content': [{'type': 'output_text', 'text': 'A=18%, B=9%, total=15%'}]}],
                'usage': {'input_tokens': 100, 'output_tokens': 20}}

    def test_worker_is_tool_free_and_receives_only_supplied_context(self):
        payload = build_payload('Analyze these authorized excerpts.', 'high')
        self.assertEqual(payload['input'], 'Analyze these authorized excerpts.')
        self.assertEqual(payload['tools'], [])
        self.assertEqual(payload['tool_choice'], 'none')
        self.assertFalse(payload['stream'])
        self.assertNotIn('previous_response_id', payload)

    def test_complete_reply_reports_transport_evidence(self):
        result = verified_result(self.response(), 'high')
        self.assertEqual(result['observed_model'], 'gemini-4-argon-high')
        self.assertEqual(result['reply'], 'A=18%, B=9%, total=15%')
        self.assertEqual(result['tool_calls'], 0)

    def test_wrong_model_is_not_passed_off_as_gemini(self):
        response = self.response()
        response['metadata']['antigravity_model'] = 'different-model'
        with self.assertRaisesRegex(ValueError, 'model'):
            verified_result(response, 'high')

    def test_pending_failed_and_empty_results_are_not_success(self):
        with self.assertRaises(ValueError):
            verified_result([], 'high')
        for status in ('in_progress', 'failed', 'incomplete'):
            response = self.response()
            response['status'] = status
            with self.assertRaises(ValueError):
                verified_result(response, 'high')
        response = self.response()
        response['output'] = []
        with self.assertRaises(ValueError):
            verified_result(response, 'high')

    def test_tool_requests_are_rejected(self):
        response = self.response()
        response['output'].append({'type': 'function_call', 'name': 'exec'})
        with self.assertRaisesRegex(ValueError, 'tool'):
            verified_result(response, 'high')

    def test_invalid_effort_and_oversized_material_fail_before_transport(self):
        with self.assertRaises(ValueError):
            build_payload('Task', 'ultra')
        with self.assertRaisesRegex(ValueError, '100000'):
            build_payload('é' * 60000, 'high')

    def test_authenticated_transport_is_fixed_to_loopback_and_closes(self):
        with tempfile.TemporaryDirectory() as temp:
            Path(temp, 'auth.json').write_text(json.dumps({'tokens': {'access_token': 'synthetic-credential'}}))
            with patch.dict('os.environ', {'CODEX_HOME': temp}), patch('gemini_delegate.http.client.HTTPConnection') as connection:
                reply = connection.return_value.getresponse.return_value
                reply.status = 200
                reply.read.return_value = json.dumps(self.response()).encode()
                result = delegate('Bounded task', 'high', 60)
                connection.assert_called_once_with('127.0.0.1', 4012, timeout=60)
                request = connection.return_value.request.call_args
                self.assertEqual(request.args, ('POST', '/v1/responses'))
                self.assertEqual(request.kwargs['headers']['Authorization'], 'Bearer synthetic-credential')
                self.assertNotIn('synthetic-credential', json.dumps(result))
                connection.return_value.close.assert_called_once()

    def test_redirect_is_never_followed_with_credentials(self):
        with tempfile.TemporaryDirectory() as temp:
            Path(temp, 'auth.json').write_text(json.dumps({'tokens': {'access_token': 'synthetic-credential'}}))
            with patch.dict('os.environ', {'CODEX_HOME': temp}), patch('gemini_delegate.http.client.HTTPConnection') as connection:
                reply = connection.return_value.getresponse.return_value
                reply.status = 302
                reply.read.return_value = b''
                with self.assertRaisesRegex(ValueError, 'HTTP 302'):
                    delegate('Bounded task', 'high', 60)
                connection.assert_called_once()
                connection.return_value.request.assert_called_once()
                connection.return_value.close.assert_called_once()


if __name__ == '__main__':
    unittest.main()
