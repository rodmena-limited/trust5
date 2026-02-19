"""Tool JSON schema definitions for LLM function-calling.

This module contains the JSON schema definitions for all tools available to
LLM agents.  The definitions are consumed by ``Tools.get_definitions()``
in ``trust5.core.tools``.
"""

from typing import Any


def build_tool_definitions() -> list[dict[str, Any]]:
    """Return the core list of tool definitions (JSON schema dicts).

    This does **not** include ``AskUserQuestion`` which is conditionally
    added by :pymeth:`Tools.get_definitions` based on interactivity mode.
    """
    return [
        {
            "type": "function",
            "function": {
                "name": "InstallPackage",
                "description": "Install a package using the project's package manager",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "package_name": {
                            "type": "string",
                            "description": "Name of package to install",
                        }
                    },
                    "required": ["package_name"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "InitProject",
                "description": "Initialize a new project structure",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "path": {
                            "type": "string",
                            "description": "Project path (default .)",
                        }
                    },
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "Read",
                "description": (
                    "Read file content. Tool results are capped at 8000 chars — "
                    "for large files, use offset and limit to read specific "
                    "line ranges instead of the whole file. "
                    "Use Grep to find line numbers first, then Read with offset."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "file_path": {
                            "type": "string",
                            "description": "Path to file",
                        },
                        "offset": {
                            "type": "integer",
                            "description": "Start reading from this line number (1-indexed). Optional.",
                        },
                        "limit": {
                            "type": "integer",
                            "description": "Maximum number of lines to return. Optional.",
                        },
                    },
                    "required": ["file_path"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "Write",
                "description": "Write content to file",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "file_path": {
                            "type": "string",
                            "description": "Path to file",
                        },
                        "content": {
                            "type": "string",
                            "description": "Content to write",
                        },
                    },
                    "required": ["file_path", "content"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "ReadFiles",
                "description": "Read multiple files at once. Returns JSON dict of path->content.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "file_paths": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "List of file paths to read",
                        }
                    },
                    "required": ["file_paths"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "Edit",
                "description": "Edit a file by replacing an exact string match. "
                "old_string must appear exactly once. Safer than Write for small changes.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "file_path": {
                            "type": "string",
                            "description": "Path to file",
                        },
                        "old_string": {
                            "type": "string",
                            "description": "Exact string to find and replace (must be unique in file)",
                        },
                        "new_string": {
                            "type": "string",
                            "description": "Replacement string",
                        },
                    },
                    "required": ["file_path", "old_string", "new_string"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "Bash",
                "description": "Run bash command",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "command": {
                            "type": "string",
                            "description": "Command to run",
                        },
                        "workdir": {
                            "type": "string",
                            "description": "Working directory",
                        },
                    },
                    "required": ["command"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "Glob",
                "description": "List files matching pattern",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "pattern": {
                            "type": "string",
                            "description": "Glob pattern",
                        },
                        "workdir": {
                            "type": "string",
                            "description": "Working directory",
                        },
                    },
                    "required": ["pattern"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "Grep",
                "description": "Search file contents for a regex pattern. Returns matching lines.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "pattern": {
                            "type": "string",
                            "description": "Regex pattern to search for",
                        },
                        "path": {
                            "type": "string",
                            "description": "Directory to search in (default .)",
                        },
                        "include": {
                            "type": "string",
                            "description": "File glob filter (e.g. '*.py')",
                        },
                    },
                    "required": ["pattern"],
                },
            },
        },
    ]


def build_ask_user_definition() -> dict[str, Any]:
    """Return the ``AskUserQuestion`` tool definition.

    This is kept separate because it is only included when the agent
    is running in interactive mode.
    """
    return {
        "type": "function",
        "function": {
            "name": "AskUserQuestion",
            "description": "Ask user a question",
            "parameters": {
                "type": "object",
                "properties": {
                    "question": {
                        "type": "string",
                        "description": "The question to ask",
                    },
                    "options": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Options",
                    },
                },
                "required": ["question"],
            },
        },
    }
