"""Independent acceptance checks for the live ZCode execution demo."""

import unittest

from clamp import clamp


class ClampAcceptance(unittest.TestCase):
    def test_valid_intervals(self):
        cases = [
            (5, 0, 3, 3),
            (-1, 0, 3, 0),
            (2, 0, 3, 2),
            (0, 0, 3, 0),
            (3, 0, 3, 3),
            (5, 2, 2, 2),
            (0.25, 0.0, 1.0, 0.25),
            (-4, -3, -1, -3),
        ]
        for value, lower, upper, expected in cases:
            with self.subTest(value=value, lower=lower, upper=upper):
                self.assertEqual(clamp(value, lower, upper), expected)

    def test_invalid_interval_raises(self):
        with self.assertRaises(ValueError):
            clamp(2, 3, 1)


if __name__ == "__main__":
    unittest.main()
