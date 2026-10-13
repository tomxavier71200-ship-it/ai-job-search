"""Tests for tools/rank_contacts.py (the /network ranking step).

Inputs mirror the shapes api-anything's bundled `linkedin searchPeople`
operation returns (CLI list of records, MCP {columns, rows}) and LinkedIn's
real Connections.csv export, including its 'Notes:' preamble.
"""
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SCRIPT = REPO / "tools" / "rank_contacts.py"
COMMAND = REPO / ".claude" / "commands" / "network.md"
sys.path.insert(0, str(REPO / "tools"))

import rank_contacts as rc  # noqa: E402

TARGET = {
    "company": "Siemens Energy",
    "aliases": ["siemens-energy"],
    "role_keywords": ["power electronics", "converter"],
    "location": "Munich",
}

RECRUITER = {
    "name": "Anna Recruiter", "headline": "Talent Acquisition Partner at Siemens Energy",
    "location": "Munich, Bavaria, Germany", "publicId": "anna-r",
    "url": "https://www.linkedin.com/in/anna-r?miniProfileUrn=x", "distance": "DISTANCE_3",
}
MANAGER = {
    "name": "Max Leiter", "headline": "Teamleiter Power Electronics | Siemens Energy",
    "location": "Erlangen, Bavaria, Germany", "publicId": "max-l",
    "url": "https://www.linkedin.com/in/max-l", "distance": "DISTANCE_2",
}
ELSEWHERE = {
    "name": "Ex Employee", "headline": "Power Electronics Engineer at Bosch",
    "location": "Munich", "publicId": "ex-e",
    "url": "https://www.linkedin.com/in/ex-e", "distance": "DISTANCE_1",
}

CONNECTIONS_CSV = """Notes:
"When exporting your connection data, you may notice that some of the email addresses are missing."

First Name,Last Name,URL,Email Address,Company,Position,Connected On
Carla,Friend,https://www.linkedin.com/in/carla-f,,Siemens Energy,Converter Design Engineer,01 Mar 2024
Dan,Other,https://www.linkedin.com/in/dan-o,,Infineon,Engineer,02 Mar 2024
"""


class Ranking(unittest.TestCase):
    def test_at_company_beats_off_company_even_if_first_degree(self):
        rows = rc.rank({**TARGET, "results": [{"tag": "peer", "data": [ELSEWHERE, RECRUITER]}]})
        self.assertEqual([r["name"] for r in rows], ["Anna Recruiter", "Ex Employee"])
        self.assertFalse(rows[1]["at_company"])

    def test_second_degree_field_manager_outranks_third_degree_recruiter(self):
        rows = rc.rank({**TARGET, "results": [{"tag": "recruiter", "data": [RECRUITER]},
                                              {"tag": "manager", "data": [MANAGER]}]})
        self.assertEqual(rows[0]["name"], "Max Leiter")
        self.assertEqual(rows[0]["type"], "manager")
        self.assertEqual(rows[1]["type"], "recruiter")

    def test_dedupes_across_queries_and_merges_tags(self):
        rows = rc.rank({**TARGET, "results": [{"tag": "recruiter", "data": [RECRUITER]},
                                              {"tag": "alumni", "data": [RECRUITER]}]})
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["found_by"], ["alumni", "recruiter"])
        self.assertTrue(any("alumni" in r for r in rows[0]["reasons"]))

    def test_accepts_mcp_columns_rows_shape(self):
        cols = list(RECRUITER)
        data = {"columns": cols, "rows": [[RECRUITER[c] for c in cols]]}
        rows = rc.rank({**TARGET, "results": [{"tag": "recruiter", "data": data}]})
        self.assertEqual(rows[0]["name"], "Anna Recruiter")
        self.assertEqual(rows[0]["url"], "https://www.linkedin.com/in/anna-r")

    def test_company_match_is_word_bounded(self):
        rows = rc.rank({"company": "ABB", "role_keywords": [],
                        "results": [{"tag": "peer", "data": [
                            {"name": "X", "headline": "Engineer at Abbott", "publicId": "x"}]}]})
        self.assertFalse(rows[0]["at_company"])


class ConnectionsExport(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.csv = Path(self.tmp.name) / "Connections.csv"
        self.csv.write_text(CONNECTIONS_CSV, encoding="utf-8")

    def tearDown(self):
        self.tmp.cleanup()

    def test_export_keeps_only_target_company_as_first_degree(self):
        rows = rc.rank({**TARGET, "results": []}, rc.load_connections(self.csv))
        self.assertEqual([r["name"] for r in rows], ["Carla Friend"])
        self.assertEqual(rows[0]["degree"], "1st")
        self.assertIn("referral", rows[0]["approach"])

    def test_export_upgrades_search_hit_to_first_degree(self):
        hit = {"name": "Carla Friend", "headline": "Converter Design Engineer at Siemens Energy",
               "publicId": "carla-f", "url": "https://www.linkedin.com/in/carla-f",
               "distance": "DISTANCE_2", "location": "Munich"}
        rows = rc.rank({**TARGET, "results": [{"tag": "peer", "data": [hit]}]},
                       rc.load_connections(self.csv))
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["degree"], "1st")
        self.assertEqual(rows[0]["location"], "Munich")

    def test_rejects_non_export_csv(self):
        bad = Path(self.tmp.name) / "bad.csv"
        bad.write_text("a,b\n1,2\n", encoding="utf-8")
        with self.assertRaises(ValueError):
            rc.load_connections(bad)


class Cli(unittest.TestCase):
    def test_cli_markdown_output(self):
        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp) / "in.json"
            src.write_text(json.dumps({**TARGET, "results": [{"tag": "manager", "data": [MANAGER]}]}),
                           encoding="utf-8")
            out = subprocess.run([sys.executable, str(SCRIPT), str(src), "--format", "md"],
                                 capture_output=True, text=True, check=True).stdout
        self.assertIn("| 1 | [Max Leiter](https://www.linkedin.com/in/max-l) | 2nd | manager |", out)

    def test_cli_missing_company_errors_to_stderr(self):
        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp) / "in.json"
            src.write_text("{}", encoding="utf-8")
            res = subprocess.run([sys.executable, str(SCRIPT), str(src)], capture_output=True, text=True)
        self.assertEqual(res.returncode, 1)
        self.assertIn("company", json.loads(res.stderr)["error"])


class CommandSpec(unittest.TestCase):
    def test_header_and_read_only_rules(self):
        text = COMMAND.read_text(encoding="utf-8")
        self.assertTrue(text.startswith("# /network"))
        self.assertIn("Nothing is ever sent", text)
        self.assertIn("never pass `--allow-writes`", text)
        self.assertIn("untrusted data", text)


if __name__ == "__main__":
    unittest.main()
