"""Deterministic JSONL peer used by tests/test_codex_app_server.py."""

import json
import os
import sys


def read_message():
    line = sys.stdin.readline()
    if not line:
        return None
    return json.loads(line)


def write_message(message):
    sys.stdout.write(json.dumps(message, separators=(",", ":")) + "\n")
    sys.stdout.flush()


def main():
    scenario = sys.argv[1]
    initialize = read_message()
    if initialize is None or initialize.get("method") != "initialize":
        return 2
    write_message({
        "id": initialize["id"],
        "result": {
            "userAgent": "fake-app-server",
            "codexHome": "/tmp/fake-codex-home",
            "apiKeyPresent": bool(os.environ.get("OPENAI_API_KEY")),
            "clientCapabilities": initialize.get("params", {}).get("capabilities"),
        },
    })
    initialized = read_message()
    if initialized is None or initialized.get("method") != "initialized":
        return 3

    delayed = None
    threads = {}
    skill_enabled = True
    mcp_servers = {
        "fixture-mcp": {
            "command": "fixture-mcp-command",
            "args": ["--safe"],
            "enabled": True,
        },
    }
    model_providers = {}
    while message := read_message():
        method = message.get("method")
        request_id = message.get("id")

        if scenario == "malformed":
            sys.stdout.write("{malformed-json\n")
            sys.stdout.flush()
            return 0
        if scenario == "exit":
            return 7
        if scenario == "hang":
            while sys.stdin.readline():
                pass
            return 0

        if method == "test/reverse":
            if delayed is None:
                delayed = message
                continue
            write_message({"id": request_id, "result": message["params"]})
            write_message({"id": delayed["id"], "result": delayed["params"]})
            delayed = None
        elif method == "thread/start":
            thread_id = f"thread-{len(threads) + 1}"
            thread = {
                "id": thread_id,
                "name": None,
                "cwd": message.get("params", {}).get("cwd", "/tmp/fixture"),
                "status": "idle",
                "turns": [],
                "createdAt": 1,
                "updatedAt": 1,
                "ephemeral": bool(message.get("params", {}).get("ephemeral")),
                "modelProvider": message.get("params", {}).get("modelProvider", "openai"),
            }
            threads[thread_id] = thread
            write_message({"id": request_id, "result": {"thread": thread}})
            write_message({
                "method": "thread/started",
                "params": {"thread": thread, "threadId": thread_id},
            })
        elif method == "thread/list":
            cwd = message.get("params", {}).get("cwd")
            allowed = set(cwd) if isinstance(cwd, list) else {cwd}
            data = [thread for thread in threads.values()
                    if cwd is None or thread.get("cwd") in allowed]
            write_message({
                "id": request_id,
                "result": {"data": data, "nextCursor": None},
            })
        elif method == "thread/search":
            params = message.get("params", {})
            needle = str(params.get("searchTerm") or "").casefold()
            limit = int(params.get("limit") or 20)
            data = []
            for thread in threads.values():
                searchable = json.dumps(thread, ensure_ascii=False).casefold()
                if needle and needle not in searchable:
                    continue
                data.append({
                    "thread": thread,
                    "snippet": thread.get("name") or thread.get("preview") or "",
                })
                if len(data) >= limit:
                    break
            write_message({
                "id": request_id,
                "result": {"data": data, "nextCursor": None,
                           "backwardsCursor": None},
            })
        elif method == "thread/read":
            thread_id = message.get("params", {}).get("threadId")
            thread = threads.get(thread_id)
            if thread is None:
                write_message({
                    "id": request_id,
                    "error": {"code": -32004, "message": "Thread not found"},
                })
            else:
                write_message({"id": request_id, "result": {"thread": thread}})
        elif method == "thread/resume":
            thread_id = message.get("params", {}).get("threadId")
            thread = threads.get(thread_id)
            if thread is None:
                write_message({
                    "id": request_id,
                    "error": {"code": -32004, "message": "Thread not found"},
                })
            else:
                write_message({"id": request_id, "result": {"thread": thread}})
        elif method == "thread/name/set":
            params = message.get("params", {})
            thread = threads.get(params.get("threadId"))
            if thread is None:
                write_message({
                    "id": request_id,
                    "error": {"code": -32004, "message": "Thread not found"},
                })
            else:
                thread["name"] = params.get("name")
                write_message({"id": request_id, "result": {}})
        elif method == "thread/delete":
            thread_id = message.get("params", {}).get("threadId")
            threads.pop(thread_id, None)
            write_message({"id": request_id, "result": {}})
        elif method == "thread/fork":
            params = message.get("params", {})
            source = threads.get(params.get("threadId"))
            if source is None:
                write_message({
                    "id": request_id,
                    "error": {"code": -32004, "message": "Thread not found"},
                })
                continue
            thread_id = f"thread-{len(threads) + 1}"
            thread = json.loads(json.dumps(source))
            thread.update({
                "id": thread_id,
                "name": None,
                "cwd": params.get("cwd") or source.get("cwd"),
                "forkedFromId": source["id"],
                "createdAt": source.get("updatedAt", 1) + 1,
                "updatedAt": source.get("updatedAt", 1) + 1,
            })
            threads[thread_id] = thread
            write_message({"id": request_id, "result": {
                "thread": thread,
                "approvalPolicy": params.get("approvalPolicy", "on-request"),
                "approvalsReviewer": "user",
                "cwd": thread["cwd"],
                "model": params.get("model") or "gpt-test-codex",
                "modelProvider": "openai",
                "sandbox": params.get("sandbox", "workspace-write"),
            }})
        elif method == "model/list":
            write_message({
                "id": request_id,
                "result": {
                    "data": [{
                        "id": "fake-model",
                        "model": "gpt-test-codex",
                        "displayName": "GPT Test Codex",
                        "description": "Deterministic test model",
                        "hidden": False,
                        "isDefault": True,
                        "defaultReasoningEffort": "medium",
                        "supportedReasoningEfforts": [{
                            "reasoningEffort": "medium",
                            "description": "Medium",
                        }],
                        "serviceTiers": [{
                            "id": "priority",
                            "name": "Fast",
                            "description": "1.5x speed, increased usage",
                        }],
                    }],
                    "nextCursor": None,
                },
            })
        elif method == "skills/list":
            params = message.get("params", {})
            cwds = params.get("cwds") or ["/tmp/fixture"]
            write_message({
                "id": request_id,
                "result": {
                    "data": [{
                        "cwd": cwd,
                        "errors": [],
                        "skills": [{
                            "name": "fixture-skill",
                            "description": "Fixture Codex skill",
                            "shortDescription": None,
                            "path": os.path.join(
                                cwd, ".codex", "skills", "fixture-skill", "SKILL.md"),
                            "scope": "repo",
                            "enabled": skill_enabled,
                            "dependencies": None,
                            "interface": {
                                "displayName": "Fixture Skill",
                                "shortDescription": "Short fixture description",
                                "defaultPrompt": "Use fixture-skill to inspect this.",
                                "brandColor": None,
                                "iconSmall": None,
                                "iconLarge": None,
                            },
                        }],
                    } for cwd in cwds],
                },
            })
        elif method == "skills/config/write":
            skill_enabled = bool(message.get("params", {}).get("enabled"))
            write_message({
                "id": request_id,
                "result": {"effectiveEnabled": skill_enabled},
            })
        elif method == "config/read":
            write_message({
                "id": request_id,
                "result": {
                    "config": {
                        "mcp_servers": mcp_servers,
                        "model_providers": model_providers,
                    },
                    "origins": {},
                    "layers": [{
                        "name": {"type": "user", "file": "/tmp/fake-codex-home/config.toml"},
                        "version": "fake-version",
                        "config": {
                            "mcp_servers": mcp_servers,
                            "model_providers": model_providers,
                        },
                    }],
                },
            })
        elif method == "config/value/write":
            params = message.get("params", {})
            parts = params.get("keyPath", "").split(".")
            value = params.get("value")
            if len(parts) == 2 and parts[0] == "mcp_servers":
                if value is None:
                    mcp_servers.pop(parts[1], None)
                else:
                    mcp_servers[parts[1]] = value
            elif len(parts) == 3 and parts[0] == "mcp_servers" and parts[2] == "enabled":
                mcp_servers.setdefault(parts[1], {})["enabled"] = value
            elif len(parts) == 2 and parts[0] == "model_providers":
                if value is None:
                    model_providers.pop(parts[1], None)
                else:
                    model_providers[parts[1]] = value
            else:
                write_message({
                    "id": request_id,
                    "error": {"code": -32602, "message": "Invalid config key"},
                })
                continue
            write_message({
                "id": request_id,
                "result": {
                    "filePath": "/tmp/fake-codex-home/config.toml",
                    "status": "ok",
                    "version": "fake-version-2",
                },
            })
        elif method == "config/mcpServer/reload":
            write_message({"id": request_id, "result": {}})
        elif method == "mcpServerStatus/list":
            data = []
            for name, spec in mcp_servers.items():
                enabled = spec.get("enabled", True) is not False
                tools = {
                    "fixture_tool": {
                        "name": "fixture_tool",
                        "title": "Fixture tool",
                        "description": "A deterministic MCP tool",
                        "inputSchema": {"type": "object"},
                    },
                } if enabled else {}
                data.append({
                    "name": name,
                    "authStatus": "notLoggedIn" if spec.get("url") else "unsupported",
                    "tools": tools,
                    "resources": [],
                    "resourceTemplates": [],
                    "serverInfo": {
                        "name": name,
                        "title": "Fixture MCP",
                        "version": "1.0.0",
                    } if enabled else None,
                })
            write_message({
                "id": request_id,
                "result": {"data": data, "nextCursor": None},
            })
        elif method == "mcpServer/oauth/login":
            name = message.get("params", {}).get("name", "fixture")
            write_message({
                "id": request_id,
                "result": {"authorizationUrl": f"https://auth.example.test/{name}"},
            })
        elif method == "thread/settings/update":
            write_message({"id": request_id, "result": {}})
        elif method == "turn/start":
            params = message.get("params", {})
            thread_id = params.get("threadId", "thread-1")
            thread = threads.get(thread_id)
            turn_id = f"turn-{len(thread.get('turns', [])) + 1}" if thread else "turn-1"
            turn_input = [
                part for part in params.get("input", []) if isinstance(part, dict)
            ]
            prompt = "".join(
                part.get("text", "")
                for part in turn_input if part.get("type") == "text"
            )
            turn = {"id": turn_id, "status": "inProgress", "items": []}
            write_message({"id": request_id, "result": {"turn": turn}})
            write_message({
                "method": "turn/started",
                "params": {"threadId": thread_id, "turn": turn},
            })
            write_message({
                "method": "item/reasoning/summaryTextDelta",
                "params": {
                    "threadId": thread_id,
                    "turnId": turn_id,
                    "itemId": "reasoning-1",
                    "summaryIndex": 0,
                    "delta": "thinking briefly",
                },
            })
            command_item = {
                "id": "command-1",
                "type": "commandExecution",
                "command": "pwd",
                "commandActions": [],
                "cwd": thread.get("cwd", "/tmp/fixture") if thread else "/tmp/fixture",
                "status": "inProgress",
                "aggregatedOutput": None,
            }
            write_message({
                "method": "item/started",
                "params": {
                    "threadId": thread_id,
                    "turnId": turn_id,
                    "item": command_item,
                    "startedAtMs": 1,
                },
            })
            for delta in ("hello ", "from Codex"):
                write_message({
                    "method": "item/agentMessage/delta",
                    "params": {
                        "threadId": thread_id,
                        "turnId": turn_id,
                        "itemId": "item-1",
                        "delta": delta,
                    },
                })
            write_message({
                "method": "thread/tokenUsage/updated",
                "params": {
                    "threadId": thread_id,
                    "turnId": turn_id,
                    "tokenUsage": {
                        "total": {
                            "totalTokens": 12,
                            "inputTokens": 9,
                            "cachedInputTokens": 4,
                            "outputTokens": 3,
                            "reasoningOutputTokens": 1,
                        },
                        "last": {
                            "totalTokens": 10,
                            "inputTokens": 8,
                            "cachedInputTokens": 4,
                            "outputTokens": 2,
                            "reasoningOutputTokens": 1,
                        },
                        "modelContextWindow": 100,
                    },
                },
            })

            decisions = []
            if params.get("approvalPolicy") != "never":
                for server_id, approval_method in (
                    ("server-command", "item/commandExecution/requestApproval"),
                    ("server-file", "item/fileChange/requestApproval"),
                ):
                    write_message({
                        "id": server_id,
                        "method": approval_method,
                        "params": {
                            "threadId": thread_id,
                            "turnId": turn_id,
                            "itemId": server_id,
                            "command": "pwd" if server_id == "server-command" else None,
                            "startedAtMs": 1,
                        },
                    })
                    response = read_message()
                    decisions.append(response.get("result", {}).get("decision"))

            user_input_answers = {}
            if "request user input" in prompt.lower():
                write_message({
                    "id": "server-input",
                    "method": "item/tool/requestUserInput",
                    "params": {
                        "threadId": thread_id,
                        "turnId": turn_id,
                        "itemId": "input-1",
                        "questions": [{
                            "id": "scope",
                            "header": "Scope",
                            "question": "Which scope should Codex use?",
                            "options": [
                                {
                                    "label": "Current file",
                                    "description": "Limit the change to one file.",
                                },
                                {
                                    "label": "Whole project",
                                    "description": "Apply the change across the project.",
                                },
                            ],
                        }],
                    },
                })
                response = read_message()
                user_input_answers = response.get("result", {}).get("answers", {})

            command_item = dict(command_item)
            command_item.update({
                "status": "completed",
                "aggregatedOutput": "/tmp/fixture\n",
                "exitCode": 0,
            })
            write_message({
                "method": "item/completed",
                "params": {
                    "threadId": thread_id,
                    "turnId": turn_id,
                    "item": command_item,
                    "completedAtMs": 2,
                },
            })

            completed_items = [
                {
                    "id": "user-1",
                    "type": "userMessage",
                    "content": turn_input,
                    "clientId": params.get("clientUserMessageId"),
                },
                {
                    "id": "reasoning-1",
                    "type": "reasoning",
                    "summary": ["thinking briefly"],
                    "content": [],
                },
                {
                    "id": "item-1",
                    "type": "agentMessage",
                    "text": "hello from Codex",
                },
            ]
            completed = {
                "id": turn_id,
                "status": "completed",
                "items": completed_items,
                "durationMs": 5,
                "approvalDecisions": decisions,
                "userInputAnswers": user_input_answers,
            }
            if thread is not None:
                thread["turns"].append(completed)
                thread["updatedAt"] += 1
                thread["preview"] = thread.get("preview") or prompt
            write_message({
                "method": "turn/completed",
                "params": {"threadId": thread_id, "turn": completed},
            })
        elif method == "thread/compact/start":
            thread_id = message.get("params", {}).get("threadId")
            thread = threads.get(thread_id)
            if thread is None:
                write_message({
                    "id": request_id,
                    "error": {"code": -32004, "message": "Thread not found"},
                })
                continue
            turn_id = f"compact-{len(thread.get('turns', [])) + 1}"
            item = {"id": "compact-1", "type": "contextCompaction"}
            turn = {"id": turn_id, "status": "inProgress", "items": []}
            write_message({"id": request_id, "result": {}})
            write_message({
                "method": "turn/started",
                "params": {"threadId": thread_id, "turn": turn},
            })
            write_message({
                "method": "item/started",
                "params": {"threadId": thread_id, "turnId": turn_id, "item": item},
            })
            write_message({
                "method": "thread/tokenUsage/updated",
                "params": {
                    "threadId": thread_id,
                    "turnId": turn_id,
                    "tokenUsage": {
                        "total": {
                            "totalTokens": 20,
                            "inputTokens": 15,
                            "cachedInputTokens": 4,
                            "outputTokens": 5,
                            "reasoningOutputTokens": 1,
                        },
                        "last": {
                            "totalTokens": 4,
                            "inputTokens": 0,
                            "cachedInputTokens": 0,
                            "outputTokens": 0,
                            "reasoningOutputTokens": 0,
                        },
                        "modelContextWindow": 100,
                    },
                },
            })
            write_message({
                "method": "item/completed",
                "params": {"threadId": thread_id, "turnId": turn_id, "item": item},
            })
            completed = {"id": turn_id, "status": "completed", "items": [item]}
            thread["turns"].append(completed)
            thread["updatedAt"] += 1
            write_message({
                "method": "turn/completed",
                "params": {"threadId": thread_id, "turn": completed},
            })
        elif method == "turn/interrupt":
            write_message({"id": request_id, "result": {}})
        else:
            write_message({
                "id": request_id,
                "error": {"code": -32601, "message": "Method not found"},
            })
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
