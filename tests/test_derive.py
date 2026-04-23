"""
Tests for lob_sender.derive.

Run with:  python -m unittest tests.test_derive
"""
from __future__ import annotations

import unittest
from datetime import date

from lob_sender.derive import (
    derive_first_name,
    derive_violation_subject,
    parse_mailing_address,
    derive_for_row,
    GENERIC_SALUTATION,
)


class TestFirstName(unittest.TestCase):
    def test_simple(self):
        self.assertEqual(derive_first_name("RAUL MUNOZ FRANCO"), "RAUL")

    def test_multi_owner_comma(self):
        self.assertEqual(
            derive_first_name("MORITZ ESSER, YUDIT VIRGINIA PINA RODRIGUEZ"),
            GENERIC_SALUTATION,
        )

    def test_multi_owner_slash(self):
        self.assertEqual(
            derive_first_name("Yamil Horruitinel / Andres F Salazar"),
            GENERIC_SALUTATION,
        )

    def test_multi_owner_ampersand(self):
        self.assertEqual(
            derive_first_name("JOHN DOE & JANE DOE"),
            GENERIC_SALUTATION,
        )

    def test_llc(self):
        self.assertEqual(derive_first_name("MIDAS REAL ESTATE LLC"), GENERIC_SALUTATION)

    def test_corp(self):
        self.assertEqual(derive_first_name("ACME PROPERTIES INC"), GENERIC_SALUTATION)

    def test_trust(self):
        self.assertEqual(derive_first_name("SMITH FAMILY TR"), GENERIC_SALUTATION)

    def test_empty(self):
        self.assertEqual(derive_first_name(None), GENERIC_SALUTATION)
        self.assertEqual(derive_first_name(""), GENERIC_SALUTATION)


class TestViolationSubject(unittest.TestCase):
    def test_single_keyword(self):
        self.assertEqual(derive_violation_subject("door", None), "door")

    def test_durafence_normalizes_to_fence(self):
        self.assertEqual(derive_violation_subject("durafence", None), "fence")

    def test_two_subjects_joined_with_and(self):
        self.assertEqual(
            derive_violation_subject("fence,door", None),
            "fence and door",
        )

    def test_three_subjects_oxford_comma(self):
        result = derive_violation_subject("fence,gate,electrical", None)
        self.assertEqual(result, "fence, gate, and electrical work")

    def test_falls_back_to_violation_text(self):
        # No keywords matched, but raw violation text mentions a window
        self.assertEqual(
            derive_violation_subject(None, "Owner installed new windows"),
            "window",
        )

    def test_default_when_nothing_matches(self):
        self.assertEqual(derive_violation_subject(None, None), "property")
        self.assertEqual(derive_violation_subject("", "irrelevant text"), "property")


class TestMailingAddressParse(unittest.TestCase):
    def test_miami_dade_format(self):
        # Note the stray space before the comma — common in MD exports
        result = parse_mailing_address("8101 SW 72ND AVE 404W , MIAMI FL 33143")
        self.assertEqual(result["address_line1"], "8101 SW 72ND AVE 404W")
        self.assertEqual(result["address_city"],  "MIAMI")
        self.assertEqual(result["address_state"], "FL")
        self.assertEqual(result["address_zip"],   "33143")

    def test_homestead_format(self):
        result = parse_mailing_address("1828 Se 18 Ter, Homestead FL 33035")
        self.assertEqual(result["address_line1"], "1828 Se 18 Ter")
        self.assertEqual(result["address_city"],  "HOMESTEAD")
        self.assertEqual(result["address_zip"],   "33035")

    def test_zip_plus_four(self):
        result = parse_mailing_address("11070 NW 22ND CT , MIAMI FL 33167-3053")
        self.assertEqual(result["address_zip"], "33167-3053")

    def test_returns_none_for_unparseable(self):
        self.assertIsNone(parse_mailing_address(""))
        self.assertIsNone(parse_mailing_address(None))
        self.assertIsNone(parse_mailing_address("just some random text no zip"))


class TestDeriveForRow(unittest.TestCase):
    def test_miami_dade_complete(self):
        row = {
            "source": "miami_dade_unincorporated",
            "case_number": "20260247179",
            "owner_full_name": "RAUL MUNOZ FRANCO",
            "owner_mailing_address": "8101 SW 72ND AVE 404W , MIAMI FL 33143",
            "property_address": "8101 SW 72 AVE 404W",
            "matched_keywords": "door",
            "alleged_violation": "Door replacement.",
        }
        out = derive_for_row(row, today=date(2026, 4, 23))
        self.assertEqual(out["errors"], [])
        self.assertEqual(out["to_address"]["name"], "RAUL MUNOZ FRANCO")
        self.assertEqual(out["to_address"]["address_zip"], "33143")
        self.assertEqual(out["merge_variables"]["jurisdiction"], "Miami-Dade County")
        self.assertEqual(out["merge_variables"]["first_name"], "RAUL")
        self.assertEqual(out["merge_variables"]["violation_subject"], "door")
        self.assertEqual(out["merge_variables"]["date"], "April 23, 2026")

    def test_homestead_multi_owner(self):
        row = {
            "source": "homestead",
            "case_number": "CC-26-00090-NOV",
            "owner_full_name": "Yamil Horruitinel / Andres F Salazar",
            "owner_mailing_address": "1920 Se 18 St, Homestead FL 33035",
            "property_address": "1920 SE 18TH ST",
            "matched_keywords": "(pre-filtered)",
            "alleged_violation": "FRONT DOOR INSTALLED NO PERMIT ON FILE",
        }
        out = derive_for_row(row, today=date(2026, 4, 23))
        self.assertEqual(out["errors"], [])
        # First owner only on the envelope
        self.assertEqual(out["to_address"]["name"], "Yamil Horruitinel")
        # But salutation is generic because there were multiple owners
        self.assertEqual(out["merge_variables"]["first_name"], "Property Owner")
        self.assertEqual(out["merge_variables"]["jurisdiction"], "the City of Homestead")
        # Subject pulled from violation text via fallback
        self.assertEqual(out["merge_variables"]["violation_subject"], "door")

    def test_missing_mailing_address_records_error(self):
        row = {
            "source": "homestead",
            "case_number": "CC-26-XXXX",
            "owner_full_name": None,
            "owner_mailing_address": None,
            "property_address": None,
            "matched_keywords": None,
            "alleged_violation": None,
        }
        out = derive_for_row(row)
        self.assertIn("unparseable_mailing_address", out["errors"])
        self.assertIn("missing_owner_name", out["errors"])

    def test_unknown_source_uses_generic_jurisdiction(self):
        row = {
            "source": "some_new_city",
            "case_number": "X1",
            "owner_full_name": "JANE DOE",
            "owner_mailing_address": "100 MAIN ST, ANYTOWN FL 33000",
            "property_address": "100 MAIN ST",
            "matched_keywords": "fence",
            "alleged_violation": "",
        }
        out = derive_for_row(row)
        self.assertEqual(out["merge_variables"]["jurisdiction"], "your local jurisdiction")


if __name__ == "__main__":
    unittest.main()
