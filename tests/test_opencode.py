"""Node-backed execution of the generated OpenCode plugin.

The actual ``setup`` command writes ``.opencode/plugins/deepsearch-axi.js``;
these tests import that generated module with Node, drive the documented V1
``server()`` and V2 ``setup(ctx)`` APIs, and prove the plugin spawns the real
installed CLI, injects the compact TOON context into the host system array, and
captures lifecycle events without writing dashboard text to process stdout.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
from support.cli import decode, run_cli
from support.env import FAKE_KEY, isolated_env

NODE = shutil.which("node")
PLUGIN = Path(".opencode") / "plugins" / "deepsearch-axi.js"

pytestmark = pytest.mark.skipif(NODE is None, reason="Node.js is required for plugin tests")

_HARNESS = r"""
import { existsSync, readFileSync } from "node:fs";
import { pathToFileURL } from "node:url";

const [mode, pluginPath, project, child] = process.argv.slice(2);
const plugin = (await import(pathToFileURL(pluginPath).href)).default;
const result = { id: plugin.id, mode };
const statePath = `${project}/.deepsearch-axi/state.json`;
const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

async function waitForEvent(predicate, timeoutMs = 10000) {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    if (existsSync(statePath)) {
      try {
        const state = JSON.parse(readFileSync(statePath, "utf8"));
        const found = (state.sessions || []).find(predicate);
        if (found) {
          return found;
        }
      } catch {}
    }
    await sleep(25);
  }
  throw new Error("timed out waiting for lifecycle state");
}

if (mode === "v2" || mode === "v2-null") {
  let contextHandler = null;
  const queue = [];
  async function* subscribe({ signal }) {
    while (!signal.aborted) {
      if (queue.length > 0) {
        yield queue.shift();
      } else {
        await sleep(20);
      }
    }
  }
  const ctx = {
    session: {
      hook: async (name, handler) => {
        if (name === "context") {
          contextHandler = handler;
        }
      },
    },
    event: { subscribe },
  };
  const cleanup = await plugin.setup(ctx);
  const system = [];
  if (mode === "v2-null") {
    await contextHandler({ system });
  } else {
    const event = { sessionID: "node-v2-session", system };
    await contextHandler(event);
    await contextHandler(event);
  }
  result.injected = system.length;
  result.system = system;
  await waitForEvent((record) => record.event === "start");
  const sessionID = mode === "v2-null" ? "opencode-unknown" : "node-v2-session";
  queue.push({ type: "session.idle", properties: { info: { id: sessionID } } });
  const checkpoint = await waitForEvent((record) => record.event !== "start");
  result.checkpoint = checkpoint.event;
  await cleanup();
  result.cleaned = true;
} else if (mode === "v1") {
  const server = await plugin.server();
  const transform = server["experimental.chat.system.transform"];
  const system = [];
  await transform({ sessionID: "node-v1-session" }, { system });
  const seen = system.length > 0 ? system[0] : null;
  await transform({ sessionID: "node-v1-session" }, { system });
  result.injected = system.length;
  result.system = system;
  result.deduplicated = seen === system[0];
  await waitForEvent((record) => record.event === "start");
  await server.event({
    event: { type: "session.idle", properties: { info: { id: "node-v1-session" } } },
  });
  const checkpoint = await waitForEvent((record) => record.event !== "start");
  result.checkpoint = checkpoint.event;
  await server.dispose();
  result.cleaned = true;
} else if (mode === "v1-fallback" || mode === "v2-fallback") {
  const system = [];
  let cleanup;
  if (mode === "v1-fallback") {
    const server = await plugin.server({ directory: child });
    await server["experimental.chat.system.transform"](
      { sessionID: "node-fallback" }, { system },
    );
    cleanup = server.dispose;
  } else {
    let handler;
    cleanup = await plugin.setup({
      location: { directory: child },
      session: { hook: async (_, callback) => { handler = callback; } },
      event: { subscribe: async function* ({ signal }) {
        while (!signal.aborted) await sleep(10);
      } },
    });
    await handler({ sessionID: "node-fallback", system });
  }
  result.system = system;
  await cleanup();
  const records = JSON.parse(readFileSync(statePath, "utf8")).sessions;
  result.checkpoint = records[0].event;
  result.scope = records[0].scope;
  result.cleaned = true;
} else if (mode === "v1-native") {
  // Real V1 envelopes: session metadata lives under `properties.info`.
  const server = await plugin.server();
  const sessionID = "node-v1-native";
  await server.event({
    event: {
      type: "session.created",
      properties: {
        info: {
          id: sessionID,
          projectID: "prj_node_test",
          directory: child,
          title: "native child",
          version: "1.18.29",
          time: { created: 1, updated: 1 },
        },
      },
    },
  });
  const system = [];
  await server["experimental.chat.system.transform"]({ sessionID }, { system });
  result.injected = system.length;
  result.system = system;
  await waitForEvent((record) => record.event === "start");
  await server.event({
    event: { type: "session.idle", properties: { sessionID } },
  });
  const checkpoint = await waitForEvent((record) => record.event === "checkpoint");
  result.checkpoint = checkpoint.event;
  result.scope = checkpoint.scope;
  await server.dispose();
  result.cleaned = true;
} else if (mode === "v2-native") {
  // Real V2 envelopes: events carry their payload under `data`, and the
  // session directory arrives on `data.location.directory`. The context hook
  // can run before the subscription observes session.created, so the host's
  // session lookup supplies the child directory.
  let contextHandler = null;
  const queue = [];
  async function* subscribe({ signal }) {
    while (!signal.aborted) {
      if (queue.length > 0) {
        yield queue.shift();
      } else {
        await sleep(20);
      }
    }
  }
  const sessionID = "node-v2-native";
  const ctx = {
    session: {
      hook: async (name, handler) => {
        if (name === "context") {
          contextHandler = handler;
        }
      },
      get: async ({ sessionID: requested }) => ({
        id: requested,
        location: { directory: child },
      }),
    },
    event: { subscribe },
  };
  const cleanup = await plugin.setup(ctx);
  const system = [];
  await contextHandler({ sessionID, system });
  result.injected = system.length;
  result.system = system;
  queue.push({
    id: "evt_node_created",
    created: 1,
    type: "session.created",
    durable: { aggregateID: sessionID, seq: 1, version: 1 },
    data: {
      sessionID,
      projectID: "prj_node_test",
      location: { directory: child },
      slug: "native-child",
      version: "2.0.0",
    },
  });
  await waitForEvent((record) => record.event === "start");
  queue.push({
    id: "evt_node_idle",
    created: 2,
    type: "session.idle",
    data: { sessionID },
  });
  const checkpoint = await waitForEvent((record) => record.event === "checkpoint");
  result.checkpoint = checkpoint.event;
  result.scope = checkpoint.scope;
  await cleanup();
  result.cleaned = true;
} else {
  throw new Error(`unknown mode: ${mode}`);
}

process.stdout.write(JSON.stringify(result));
"""


def _fake_console(home: Path) -> Path:
    script = home / "bin" / "deepsearch-axi"
    script.parent.mkdir(parents=True)
    script.write_text(
        f"""#!{sys.executable}
import os

from deepsearch_axi._entry import main

_log = os.environ.get("DEEPSEARCH_AXI_TEST_CWD")
if _log:
    with open(_log, "a", encoding="utf-8") as _handle:
        _handle.write(os.getcwd() + "\\n")
raise SystemExit(main())
""",
        encoding="utf-8",
    )
    script.chmod(0o755)
    return script


def _environment(home: Path, cwd_log: Path | None = None) -> dict[str, str]:
    environment = isolated_env(home, DEEPSEEK_API_KEY=FAKE_KEY)
    # The plugin records the absolute console-script path in this test.
    environment["PATH"] = os.environ.get("PATH", "")
    if cwd_log is not None:
        environment["DEEPSEARCH_AXI_TEST_CWD"] = str(cwd_log)
    return environment


def _generate(executable: Path, environment: dict[str, str], project: Path) -> Path:
    run = run_cli(
        ["setup", "--agent", "opencode"],
        env=environment,
        cwd=project,
        executable=[str(executable)],
    )
    assert run.code == 0, (run.stdout, run.stderr)
    assert run.stderr == ""
    # Node decides module type from the nearest package.json; real OpenCode
    # loads the plugin with its own bundler, while Node needs this marker.
    (project / "package.json").write_text('{"type": "module"}\n', encoding="utf-8")
    plugin = project / PLUGIN
    assert plugin.is_file()
    return plugin


def _run_plugin(
    mode: str,
    plugin: Path,
    project: Path,
    environment: dict[str, str],
    harness: Path,
    child: Path | None = None,
) -> dict:
    completed = subprocess.run(
        [
            NODE or "node",
            str(harness),
            mode,
            str(plugin),
            str(project),
            str(child or project),
        ],
        cwd=str(project),
        env=environment,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        timeout=120,
    )
    assert completed.returncode == 0, completed.stderr.decode("utf-8", "replace")
    assert completed.stderr == b""
    return json.loads(completed.stdout.decode("utf-8"))


def _session_records(project: Path) -> list[dict]:
    state = json.loads((project / ".deepsearch-axi" / "state.json").read_text(encoding="utf-8"))
    return state["sessions"]


def test_generated_plugin_v2_injects_context_and_captures_checkpoint(
    tmp_path: Path, home_dir: Path, project_dir: Path
) -> None:
    executable = _fake_console(home_dir)
    environment = _environment(home_dir)
    plugin = _generate(executable, environment, project_dir)
    harness = tmp_path / "harness.mjs"
    harness.write_text(_HARNESS, encoding="utf-8")

    result = _run_plugin("v2", plugin, project_dir, environment, harness)
    assert result["id"] == "deepsearch-axi"
    assert result["injected"] == 1, "the same system array must not be injected twice"
    assert result["checkpoint"] == "checkpoint"
    assert result["cleaned"] is True

    document = decode(result["system"][0]["text"])
    assert document["cwd"] == str(project_dir.resolve())
    assert document["integration"] == "installed"
    assert document["auth"] == {"state": "configured"}
    assert FAKE_KEY not in result["system"][0]["text"]

    records = _session_records(project_dir)
    assert len(records) == 1
    assert records[0]["agent"] == "opencode"
    assert records[0]["scope"] == "."
    assert records[0]["event"] in {"checkpoint", "end"}
    assert "node-v2-session" not in json.dumps(records)


def test_generated_plugin_v2_injects_unknown_session_context(
    tmp_path: Path, home_dir: Path, project_dir: Path
) -> None:
    executable = _fake_console(home_dir)
    environment = _environment(home_dir)
    plugin = _generate(executable, environment, project_dir)
    harness = tmp_path / "harness.mjs"
    harness.write_text(_HARNESS, encoding="utf-8")

    result = _run_plugin("v2-null", plugin, project_dir, environment, harness)
    assert result["injected"] == 1
    document = decode(result["system"][0]["text"])
    assert document["cwd"] == str(project_dir.resolve())
    assert document["integration"] == "installed"
    assert result["checkpoint"] in {"checkpoint", "end"}


def test_generated_plugin_v1_server_transforms_and_captures_events(
    tmp_path: Path, home_dir: Path, project_dir: Path
) -> None:
    executable = _fake_console(home_dir)
    environment = _environment(home_dir)
    plugin = _generate(executable, environment, project_dir)
    harness = tmp_path / "harness.mjs"
    harness.write_text(_HARNESS, encoding="utf-8")

    result = _run_plugin("v1", plugin, project_dir, environment, harness)
    assert result["id"] == "deepsearch-axi"
    assert result["injected"] == 1
    assert result["deduplicated"] is True
    assert result["checkpoint"] == "checkpoint"
    assert result["cleaned"] is True

    text = result["system"][0]
    assert isinstance(text, str)
    document = decode(text)
    assert document["cwd"] == str(project_dir.resolve())
    assert document["integration"] == "installed"
    assert "node-v1-session" not in text

    records = _session_records(project_dir)
    assert records[0]["agent"] == "opencode"
    assert records[0]["scope"] == "."


def _native_child(project_dir: Path) -> Path:
    child = project_dir / "packages" / "agent-child"
    child.mkdir(parents=True)
    return child


def _assert_child_capture(
    result: dict,
    project_dir: Path,
    child: Path,
    session_id: str,
    cwd_log: Path,
) -> None:
    expected = str(child.resolve())
    text = result["system"][0]
    if isinstance(text, dict):
        text = text["text"]
    document = decode(text)
    assert document["cwd"] == expected
    assert document["integration"] == "installed"

    # The native session directory is the spawned CLI's real process cwd, not
    # the recorded project root.
    lines = cwd_log.read_text(encoding="utf-8").splitlines()
    assert len(lines) >= 2
    assert set(lines) == {expected}

    assert result["checkpoint"] == "checkpoint"
    assert result["scope"] == "packages/agent-child"
    assert result["cleaned"] is True

    records = _session_records(project_dir)
    assert len(records) == 1
    assert records[0]["agent"] == "opencode"
    assert records[0]["scope"] == "packages/agent-child"
    assert records[0]["event"] == "checkpoint"
    assert session_id not in json.dumps(records)


def test_generated_plugin_v1_real_envelopes_use_native_child_directory(
    tmp_path: Path, home_dir: Path, project_dir: Path
) -> None:
    executable = _fake_console(home_dir)
    cwd_log = tmp_path / "cwd.log"
    environment = _environment(home_dir, cwd_log)
    plugin = _generate(executable, environment, project_dir)
    harness = tmp_path / "harness.mjs"
    harness.write_text(_HARNESS, encoding="utf-8")
    child = _native_child(project_dir)
    # The setup invocation above ran from the project root; only plugin spawns
    # from here on prove the native child cwd.
    cwd_log.unlink()

    result = _run_plugin("v1-native", plugin, project_dir, environment, harness, child)

    assert result["id"] == "deepsearch-axi"
    assert result["injected"] == 1
    _assert_child_capture(result, project_dir, child, "node-v1-native", cwd_log)


@pytest.mark.parametrize("mode", ["v1-fallback", "v2-fallback"])
def test_native_instance_child_directory_survives_cleanup(
    tmp_path: Path, home_dir: Path, project_dir: Path, mode: str
) -> None:
    executable = _fake_console(home_dir)
    cwd_log = tmp_path / "cwd.log"
    environment = _environment(home_dir, cwd_log)
    plugin = _generate(executable, environment, project_dir)
    harness = tmp_path / "harness.mjs"
    harness.write_text(_HARNESS, encoding="utf-8")
    child = _native_child(project_dir)
    cwd_log.unlink()

    result = _run_plugin(mode, plugin, project_dir, environment, harness, child)

    _assert_child_capture(result, project_dir, child, "node-fallback", cwd_log)


def test_generated_plugin_v2_real_envelopes_use_native_child_directory(
    tmp_path: Path, home_dir: Path, project_dir: Path
) -> None:
    executable = _fake_console(home_dir)
    cwd_log = tmp_path / "cwd.log"
    environment = _environment(home_dir, cwd_log)
    plugin = _generate(executable, environment, project_dir)
    harness = tmp_path / "harness.mjs"
    harness.write_text(_HARNESS, encoding="utf-8")
    child = _native_child(project_dir)
    cwd_log.unlink()

    result = _run_plugin("v2-native", plugin, project_dir, environment, harness, child)

    assert result["id"] == "deepsearch-axi"
    assert result["injected"] == 1
    _assert_child_capture(result, project_dir, child, "node-v2-native", cwd_log)
