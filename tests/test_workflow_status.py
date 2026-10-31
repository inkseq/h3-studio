"""#817: internal DBOS reads stay inside the frozen-catalog contract."""

import unittest

from studio_platform import workflow_status
from studio_platform.workflow_schema import frozen_catalog


class WorkflowStatusContractTests(unittest.TestCase):
    def test_every_column_read_is_in_the_frozen_dbos_catalog(self):
        columns = {
            row["attname"]
            for row in frozen_catalog()["columns"]
            if row["relname"] == "workflow_status"
        }
        self.assertTrue(columns)
        self.assertLessEqual(workflow_status.COLUMNS, columns)

    def test_only_the_adapter_names_the_internal_table(self):
        from pathlib import Path

        root = Path(workflow_status.__file__).parent
        users = sorted(
            path.name
            for path in root.rglob("*.py")
            if "inkseq_workflows.workflow_status" in path.read_text(encoding="utf-8")
        )
        self.assertEqual(users, ["workflow_status.py"])


if __name__ == "__main__":
    unittest.main()
