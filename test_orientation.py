import unittest
from orientation import arrival_bearing, event_bearing, preferred_routes
import json


def route(latitude, longitude):
    return {
        "route_kind": "reconstructed",
        "source_url": "https://example.com/report",
        "departure": {
            "latitude": latitude,
            "longitude": longitude,
            "source_url": "https://example.com/origin",
        },
    }


class RouteDirectionTests(unittest.TestCase):
    def test_cardinal_directions(self):
        cases = [((0, 0), (0, 30), 90), ((0, 30), (0, 0), 270),
                 ((0, 0), (30, 0), 0), ((30, 0), (0, 0), 180)]
        for origin, end, expected in cases:
            with self.subTest(origin=origin, end=end):
                self.assertEqual(arrival_bearing(dict(zip(("latitude", "longitude"), end)), route(*origin)), expected)

    def test_date_line_crossing(self):
        self.assertEqual(arrival_bearing({"latitude": 0, "longitude": -179}, route(0, 179)), 90)
        self.assertEqual(arrival_bearing({"latitude": 0, "longitude": 179}, route(0, -179)), 270)

    def test_ambiguous_routes_keep_neutral_direction(self):
        event = {"latitude": 0, "longitude": 30}
        self.assertIsNone(event_bearing(event, [route(0, 0), route(20, 20)]))
        self.assertIsNone(event_bearing(event, []))

    def test_undefined_geographic_directions(self):
        for latitude, longitude in [(0, 0), (0, 0.00001), (0, 180), (90, 0)]:
            self.assertIsNone(arrival_bearing({"latitude": latitude, "longitude": longitude}, route(0, 0)))

    def test_invalid_coordinates(self):
        for latitude in [None, True, float("nan"), float("inf"), "1", 91]:
            self.assertIsNone(arrival_bearing({"latitude": 0, "longitude": 30}, route(latitude, 0)))

    def test_source_evidence_is_required(self):
        for source in [None, "javascript:alert(1)", "not a URL"]:
            candidate = route(0, 0)
            candidate["source_url"] = source
            self.assertIsNone(arrival_bearing({"latitude": 0, "longitude": 30}, candidate))

    def test_reviewed_route_precedence(self):
        reviewed, other = route(0, 0), route(20, 20)
        self.assertEqual(preferred_routes([("curated", json.dumps(reviewed)), ("NTSB", json.dumps(other))]), [reviewed])

    def test_malformed_route_payload(self):
        self.assertEqual(preferred_routes([("curated", "invalid JSON")]), [])


if __name__ == "__main__":
    unittest.main()
