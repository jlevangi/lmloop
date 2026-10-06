"""Unit tests for decision_model.py."""

import json
import unittest
from unittest.mock import MagicMock, patch

import decision_model


class DecisionModelTests(unittest.TestCase):
    def test_build_systemone_request_structure(self):
        state = {"iteration": 5, "last_tool": "read", "tool_calls": 12}
        req = decision_model.build_systemone_request(state, model="clef-flash")
        self.assertEqual("clef-flash", req["model"])
        self.assertEqual(state, req["state"])
        self.assertIn("trajectory", req["questions"])
        self.assertIn("recommended_action", req["questions"])
        self.assertIn("urgency", req["questions"])
        self.assertEqual("choice", req["questions"]["trajectory"]["type"])
        self.assertEqual("choice", req["questions"]["recommended_action"]["type"])
        self.assertEqual("score", req["questions"]["urgency"]["type"])

    def test_parse_systemone_response(self):
        sample_resp = {
            "model": "clef-flash",
            "answers": {
                "trajectory": {
                    "type": "choice",
                    "choice": "spinning",
                    "confidence": 0.94,
                    "probabilities": {"healthy": 0.04, "spinning": 0.94, "failing": 0.02},
                },
                "recommended_action": {
                    "type": "choice",
                    "choice": "steer",
                    "confidence": 0.88,
                    "probabilities": {"continue": 0.08, "steer": 0.88, "interrupt": 0.04},
                },
                "urgency": {
                    "type": "score",
                    "score": 1.25,
                    "confidence": 0.85,
                    "probabilities": {"0": 0.05, "1": 0.65, "2": 0.30},
                },
            },
        }
        res = decision_model.parse_systemone_response(sample_resp)
        self.assertEqual("spinning", res.trajectory)
        self.assertEqual("steer", res.recommended_action)
        self.assertEqual(1.25, res.urgency)
        self.assertEqual(0.88, res.confidence)

    def test_steer_prompt_formats(self):
        state = {"repeated": "read\x00config.yaml"}
        # continue -> empty
        d_cont = decision_model.DecisionResult("healthy", "continue", 0.0, 0.99, {})
        self.assertEqual("", decision_model.steer_prompt(d_cont, state))

        # spinning
        d_spin = decision_model.DecisionResult("spinning", "steer", 1.0, 0.9, {})
        msg = decision_model.steer_prompt(d_spin, state)
        self.assertIn("Steering notice", msg)
        self.assertIn("read\x00config.yaml", msg)

        # failing
        d_fail = decision_model.DecisionResult("failing", "steer", 1.5, 0.8, {})
        msg_fail = decision_model.steer_prompt(d_fail, state)
        self.assertIn("recurring check/test failures", msg_fail)

    @patch("urllib.request.urlopen")
    def test_evaluate_trajectory_success(self, mock_urlopen):
        mock_resp = MagicMock()
        mock_resp.status = 200
        mock_resp.read.return_value = json.dumps({
            "answers": {
                "trajectory": {"choice": "healthy"},
                "recommended_action": {"choice": "continue", "confidence": 0.99},
                "urgency": {"score": 0.0},
            }
        }).encode("utf-8")
        mock_resp.__enter__.return_value = mock_resp
        mock_urlopen.return_value = mock_resp

        res = decision_model.evaluate_trajectory("http://localhost:5810/v1/systemone", {"iteration": 1})
        self.assertIsNotNone(res)
        assert res is not None
        self.assertEqual("healthy", res.trajectory)
        self.assertEqual("continue", res.recommended_action)

    @patch("urllib.request.urlopen", side_effect=TimeoutError("timed out"))
    def test_evaluate_trajectory_network_error_graceful(self, mock_urlopen):
        res = decision_model.evaluate_trajectory("http://localhost:5810/v1/systemone", {"iteration": 1})
        self.assertIsNone(res)


if __name__ == "__main__":
    unittest.main()
