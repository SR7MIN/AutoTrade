import unittest
from decimal import Decimal

from autotrade.binance_rest import encode_parameters, sign_query


class SigningTests(unittest.TestCase):
    def test_signature_matches_standard_hmac_sha256_vector(self) -> None:
        query = "The quick brown fox jumps over the lazy dog"
        self.assertEqual(
            sign_query(query, "key"),
            "f7bc83f430538424b13298e6aa6fb143ef4d59a14946175997479dbc2d1a3cd8",
        )

    def test_parameter_encoding(self) -> None:
        encoded = encode_parameters(
            {"quantity": Decimal("0.001"), "reduceOnly": True, "missing": None}
        )
        self.assertEqual(encoded, "quantity=0.001&reduceOnly=true")


if __name__ == "__main__":
    unittest.main()
