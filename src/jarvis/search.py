"""Jarvis-specific BM25 search transform with improved tool descriptions."""

from __future__ import annotations

from typing import Annotated, Any

from fastmcp.server.context import Context
from fastmcp.server.transforms.search import BM25SearchTransform
from fastmcp.tools.base import Tool, ToolResult


class JarvisSearchTransform(BM25SearchTransform):
    """BM25SearchTransform with clearer search/call tool descriptions.

    Overrides the synthetic tool descriptions to make the two-step workflow
    explicit and include examples that prevent small models from pasting
    their full task into the search query.
    """

    def _make_search_tool(self) -> Tool:
        transform = self

        async def search_tools(
            query: Annotated[
                str,
                (
                    "Short keyword or phrase describing the capability you need. "
                    "Use keywords only — do NOT paste the full user request here. "
                    "Examples: 'create github issue', 'read file', 'send email', 'list commits'."
                ),
            ],
            ctx: Context = None,  # type: ignore[assignment]  # ty:ignore[invalid-parameter-default]
        ) -> str | list[dict[str, Any]]:
            """STEP 1 OF 2 — Find a tool by keyword before calling it.

            Search the available tool catalog using a short keyword or phrase.
            Returns matching tool names and their parameter schemas.

            IMPORTANT: This tool discovers tools — it does not execute them.
            After finding the right tool here, use `call_tool` to run it.

            DO pass a concise keyword or phrase:
              query="create github issue"
              query="read file contents"
              query="send slack message"
              query="list git commits"

            DO NOT paste the full user task or request as the query:
              WRONG: query="Can you create a GitHub issue titled 'Login bug' with body '...'"
              RIGHT: query="create github issue"
            """
            hidden = await transform._get_visible_tools(ctx)
            results = await transform._search(hidden, query)
            return await transform._render_results(results)

        return Tool.from_function(fn=search_tools, name=self._search_tool_name)

    def _make_call_tool(self) -> Tool:
        transform = self

        async def call_tool(
            name: Annotated[
                str,
                (
                    "Exact name of the tool to execute, as returned by search_tools. "
                    "Example: 'github_create_issue', 'filesystem_read_file'."
                ),
            ],
            arguments: Annotated[
                dict[str, Any] | None,
                (
                    "Arguments for the tool as a key/value dict. "
                    "Use the parameter schema returned by search_tools to build this. "
                    "Example: {\"title\": \"Login bug\", \"body\": \"Steps to reproduce...\"}."
                ),
            ] = None,
            ctx: Context = None,  # type: ignore[assignment]  # ty:ignore[invalid-parameter-default]
        ) -> ToolResult:
            """STEP 2 OF 2 — Execute a tool discovered via search_tools.

            Call any tool by its exact name with the required arguments.
            The tool name and parameter schema come from a prior search_tools call.

            Workflow:
              1. Call search_tools with keywords to find the right tool.
              2. Call call_tool with the tool name and arguments to run it.

            Examples:
              name="github_create_issue",
                arguments={"title": "Login bug", "body": "Steps to reproduce..."}

              name="filesystem_read_file",
                arguments={"path": "/home/user/notes.txt"}

              name="slack_send_message",
                arguments={"channel": "#general", "text": "Deploy complete"}
            """
            if name in {transform._call_tool_name, transform._search_tool_name}:
                raise ValueError(
                    f"'{name}' is a synthetic search tool and cannot be called via call_tool"
                )
            return await ctx.fastmcp.call_tool(name, arguments)

        return Tool.from_function(fn=call_tool, name=self._call_tool_name)
