"""Regression: _source_urls must order operator-supplied URLs (task
description + activation_message) AHEAD of the derived claim/mission
defaults, then dedup and cap.

Before the fix the description/activation_message URLs were appended AFTER
the claim-URL and mission target_url defaults, so a tight fetch cap starved
the operator's deliberately-named sources behind the defaults. This test
fails on the unfixed code (defaults win the cap) and passes after.
"""

import unittest

from lab_pack.research_worker import _source_urls


class _Obj:
    def __init__(self, data):
        self.data = data


class _FakeGraph:
    def __init__(self, objects):
        self._objects = objects

    def get_object(self, obj_id):
        return self._objects.get(obj_id)


def _build_graph():
    claim = _Obj({
        "text": "Claim mentioning https://claim-default.example/page",
        "metadata": {"url": "https://claim-url-default.example/x"},
    })
    branch = _Obj({
        "mission_id": "mission#1",
        "metadata": {"claim_observation_id": "observation#claim"},
    })
    mission = _Obj({"target_url": "https://mission-default.example/home"})
    return _FakeGraph({
        "observation#claim": claim,
        "branch#1": branch,
        "mission#1": mission,
    })


class SourceUrlOrderTest(unittest.TestCase):
    def test_operator_urls_lead_under_cap(self):
        graph = _build_graph()
        task_data = {
            "description": "Please look at https://operator-desc.example/here",
            "metadata": {
                "lab_branch_id": "branch#1",
                "activation_message":
                    "and also https://operator-activation.example/there",
            },
        }
        # Cap of 2 must be filled by the two operator-supplied URLs, NOT by
        # the claim/mission defaults.
        urls = _source_urls(graph, task_data, cap=2)
        self.assertEqual(
            urls,
            ["https://operator-desc.example/here",
             "https://operator-activation.example/there"],
        )

    def test_operator_urls_precede_defaults_when_uncapped(self):
        graph = _build_graph()
        task_data = {
            "description": "see https://operator-desc.example/here",
            "metadata": {
                "lab_branch_id": "branch#1",
                "activation_message":
                    "and https://operator-activation.example/there",
            },
        }
        urls = _source_urls(graph, task_data, cap=10)
        # Operator URLs must appear before any derived default.
        op_desc = urls.index("https://operator-desc.example/here")
        op_act = urls.index("https://operator-activation.example/there")
        claim_url = urls.index("https://claim-url-default.example/x")
        mission_url = urls.index("https://mission-default.example/home")
        self.assertLess(op_desc, claim_url)
        self.assertLess(op_desc, mission_url)
        self.assertLess(op_act, claim_url)
        self.assertLess(op_act, mission_url)


if __name__ == "__main__":
    unittest.main()
