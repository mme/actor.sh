"""e2e MCP: list_roles."""
from __future__ import annotations

import unittest

from e2e.harness.isolated_home import isolated_home
from e2e.harness.mcp_client import McpClient


def _text(result: dict) -> str:
    return "\n".join(
        c.get("text", "") for c in result.get("content", [])
        if c.get("type") == "text"
    )


class McpListRolesTests(unittest.TestCase):

    def test_lists_built_in_main(self):
        with isolated_home() as env:
            with McpClient(env=env.env(), cwd=env.cwd) as client:
                client.initialize()
                result = client.call_tool("list_roles")
                self.assertIn("main", _text(result))

    def test_includes_user_role(self):
        with isolated_home() as env:
            env.write_settings_kdl(
                'role "qa" {\n'
                '    description "QA role"\n'
                '    agent "claude"\n'
                '}\n'
            )
            with McpClient(env=env.env(), cwd=env.cwd) as client:
                client.initialize()
                result = client.call_tool("list_roles")
                t = _text(result)
                self.assertIn("qa", t)
                self.assertIn("QA role", t)


if __name__ == "__main__":
    unittest.main()
