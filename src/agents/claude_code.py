"""Claude Code agent backend."""

import asyncio
import json
import logging
import os
import time
from pathlib import Path
from typing import Any, Dict, Optional

from .base import AgentBackend, AgentResult

logger = logging.getLogger(__name__)


def _context_tokens(usage: Dict[str, Any]) -> int:
    """True carried-context size from a usage block.

    ``input_tokens`` alone counts only the uncached new tokens; for a resumed
    session nearly the whole transcript arrives as ``cache_read_input_tokens``.
    The compaction trigger must see the full context, so sum all three.
    """
    return (
        (usage.get("input_tokens") or 0)
        + (usage.get("cache_read_input_tokens") or 0)
        + (usage.get("cache_creation_input_tokens") or 0)
    )


CLAUDE_CODE_SKILL_STEP = """\
## 0. Load the q-kdb skill
Read .claude/skills/q-kdb/SKILL.md before writing any code. It contains
syntax rules, common errors, and idioms you will need. This step is
mandatory for every task.

"""

CLAUDE_CODE_DEFAULT_INSTRUCTIONS = """\
# Workflow

You are solving a Q/kdb+ task. Always verify your solution before finishing.

{skill_step}## 1. Write solution
Read problem.md. Write your Q function in solution.q.

## 2. Check for parse errors
Run: q solution.q -q <<< "exit 0"
If there is any error, fix solution.q and run again.

## 3. Test with examples
Pick 2-3 examples from problem.md. Test by passing solution.q as q's script
argument so the function is loaded before stdin is read:
  q solution.q -q <<< "show FUNC[arg1;arg2]; exit 0"
where FUNC is the function name from solution.q. Do not chain commands
after `\\\\l` on the same line — the system command consumes the rest of the
line and the rest of your statements become part of the file path.
If output is wrong, fix and re-test.

## 4. Iterate
Repeat steps 2-3 until the solution loads cleanly and returns correct values.
"""


class ClaudeCodeBackend(AgentBackend):
    """Backend that invokes Claude Code CLI in headless mode.

    Uses `claude -p` with --output-format json for structured output.
    Agent instructions are injected via CLAUDE.md in the workspace
    and/or --append-system-prompt-file.
    """

    @property
    def name(self) -> str:
        return "claude-code"

    @property
    def instruction_filename(self) -> str:
        return "CLAUDE.md"

    def get_default_instructions(self) -> str:
        # Only instruct the agent to load the q-kdb skill when one is actually
        # installed in the workspace. For a no-skill baseline run (no
        # --skill-dirs), step 0 vanishes so we don't tell the agent to read a
        # file that won't exist.
        skill_step = CLAUDE_CODE_SKILL_STEP if self.skill_dirs else ""
        return CLAUDE_CODE_DEFAULT_INSTRUCTIONS.format(skill_step=skill_step)

    async def invoke(
        self,
        prompt: str,
        workspace: Path,
    ) -> AgentResult:
        """Invoke claude CLI in headless mode."""
        task_id_str = workspace.name
        # In persistent mode the workspace is a fixed "session" dir, so the id
        # can't be derived from its name; fall back to -1 and let the runner
        # stamp the real task_id from the problem dict.
        try:
            parsed_task_id = int(str(task_id_str).split("_")[-1])
        except ValueError:
            parsed_task_id = -1

        # Note: Claude Code CLI 2.1+ does not expose a --max-turns flag
        # (silently accepted but ignored). The per-task --timeout is the
        # only enforced backstop. We still track self.max_turns and log
        # loudly when a task exceeds it — see the post-run check below.
        # Persistent mode needs the per-turn event stream to read the true
        # end-of-task context-window size (the compaction trigger), which the
        # single aggregate 'json' result can't provide. So stream whenever the
        # user asked to save events OR we're in persistent mode.
        want_stream = self.save_events or self.session_mode == "persistent"

        cmd = [
            "claude",
            "-p",
            prompt,
            "--model",
            self.model,
            "--output-format",
            "stream-json" if want_stream else "json",
            "--dangerously-skip-permissions",
        ]

        # Persistent mode: keep the session on disk and resume it so context
        # accumulates across tasks. Fresh mode stays fully clean-room.
        if self.session_mode == "persistent":
            if self._session_id:
                cmd += ["--resume", self._session_id]
        else:
            cmd.append("--no-session-persistence")

        # stream-json requires the CLI's --verbose flag to emit per-event records
        if want_stream:
            cmd.append("--verbose")

        # Clean-room baseline: block ALL skills and plugins from reaching the
        # agent. --disable-slash-commands disables every skill (personal
        # ~/.claude/skills, plugin skills, and workspace .claude/skills);
        # --setting-sources project,local drops user settings where
        # enabledPlugins live. The stream-json init event then reports
        # skills:[] plugins:[] slash_commands:[] and no Skill tool, which is the
        # verification signal. Auth (keychain/subscription) is unaffected.
        if self.no_skills:
            cmd += [
                "--disable-slash-commands",
                "--setting-sources",
                "project,local",
            ]

        # Note: agent instructions are written as CLAUDE.md in the workspace
        # by prepare_workspace(). Claude Code auto-reads CLAUDE.md from its
        # cwd, so no --append-system-prompt-file flag is needed.

        cmd.extend(self.extra_args)

        logger.debug(f"Invoking Claude Code for {task_id_str}")

        # Remove CLAUDECODE env var to allow nested invocations
        # (e.g. when the harness is run from within a Claude Code session)
        env = {k: v for k, v in os.environ.items() if k != "CLAUDECODE"}

        # Stream to files whenever we want the event stream (save_events or
        # persistent) — this both persists the audit trail and prevents a full
        # OS pipe buffer from deadlocking claude on large verbose output.
        events_path = workspace / "events.jsonl"
        stderr_path = workspace / "events.stderr.log"
        events_fh = open(events_path, "wb") if want_stream else None
        stderr_fh = open(stderr_path, "wb") if want_stream else None

        start_time = time.monotonic()
        try:
            process = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=events_fh if events_fh else asyncio.subprocess.PIPE,
                stderr=stderr_fh if stderr_fh else asyncio.subprocess.PIPE,
                cwd=str(workspace),
                env=env,
            )

            if events_fh:
                # stdout AND stderr stream directly to files so the OS pipe
                # buffer cannot fill (which would deadlock claude on stderr
                # writes), and partial output survives a timeout cancel.
                try:
                    await asyncio.wait_for(
                        process.wait(), timeout=self.timeout
                    )
                except asyncio.TimeoutError:
                    process.kill()
                    await process.wait()
                    raise
                events_fh.close()
                events_fh = None
                stderr_fh.close()
                stderr_fh = None
                stdout = (
                    events_path.read_text(errors="replace")
                    if events_path.exists()
                    else ""
                )
                stderr_bytes = (
                    stderr_path.read_bytes() if stderr_path.exists() else b""
                )
            else:
                stdout_bytes, stderr_bytes = await asyncio.wait_for(
                    process.communicate(),
                    timeout=self.timeout,
                )
                stdout = stdout_bytes.decode("utf-8", errors="replace")

            wall_time = time.monotonic() - start_time
            stderr = stderr_bytes.decode("utf-8", errors="replace")

            if process.returncode != 0:
                logger.warning(
                    f"Claude Code exited with code {process.returncode} "
                    f"for {task_id_str}: {stderr[:200]}"
                )

            if want_stream:
                metadata = self._parse_stream_json(stdout)
            else:
                metadata = self._parse_output(stdout)

            # Persistent mode: remember the session id so the next task can
            # --resume it. Update even if --resume forked a fresh id. (Safe:
            # persistent runs are single-threaded, so no race on self.)
            if self.session_mode == "persistent":
                self._session_id = (
                    metadata.get("session_id") or self._session_id
                )

            task_id = parsed_task_id

            # Loud warning when a task burns through more turns than we
            # asked for — the CLI doesn't enforce the cap, so we surface
            # it here for visibility.
            actual_turns = metadata.get("num_turns")
            if actual_turns is not None and actual_turns > self.max_turns:
                logger.warning(
                    f"Claude Code task {task_id_str} used {actual_turns} "
                    f"turns, exceeding the configured cap of "
                    f"{self.max_turns}. The CLI does not enforce the "
                    f"cap; only --timeout ({self.timeout}s) limits runaway."
                )

            return AgentResult(
                task_id=task_id,
                success=process.returncode == 0,
                solution_code="",  # Filled by extract_solution
                wall_time_seconds=wall_time,
                num_turns=metadata.get("num_turns"),
                input_tokens=metadata.get("input_tokens"),
                output_tokens=metadata.get("output_tokens"),
                context_tokens=metadata.get("context_tokens"),
                context_window_tokens=metadata.get("context_window_tokens"),
                cost_usd=metadata.get("cost_usd"),
                raw_output=stdout,
                error=stderr if process.returncode != 0 else None,
                workspace_path=str(workspace),
                metadata=metadata,
            )

        except asyncio.TimeoutError:
            wall_time = time.monotonic() - start_time
            task_id = parsed_task_id
            logger.warning(
                f"Claude Code timed out for {task_id_str} "
                f"after {wall_time:.1f}s"
            )
            return AgentResult(
                task_id=task_id,
                success=False,
                solution_code="",
                wall_time_seconds=wall_time,
                error=f"Timed out after {self.timeout}s",
                workspace_path=str(workspace),
            )
        finally:
            if events_fh and not events_fh.closed:
                events_fh.close()
            if stderr_fh and not stderr_fh.closed:
                stderr_fh.close()

    def _parse_output(self, stdout: str) -> Dict[str, Any]:
        """Parse Claude Code JSON output for telemetry."""
        try:
            data = json.loads(stdout)
            usage = data.get("usage", {})
            return {
                "session_id": data.get("session_id"),
                "num_turns": data.get("num_turns"),
                "input_tokens": usage.get("input_tokens"),
                "output_tokens": usage.get("output_tokens"),
                "context_tokens": _context_tokens(usage),
                "cost_usd": data.get("total_cost_usd"),
                "result_text": data.get("result", ""),
            }
        except (json.JSONDecodeError, TypeError):
            logger.debug(
                "Failed to parse Claude Code JSON output, "
                "treating as plain text"
            )
            return {"result_text": stdout}

    def _parse_stream_json(self, stdout: str) -> Dict[str, Any]:
        """Parse Claude Code stream-json (JSONL) output for telemetry.

        The final event of type 'result' carries the same aggregate fields
        as the single-shot json format.
        """
        result_event: Dict[str, Any] = {}
        # Track the last assistant turn's input side — this is the true
        # end-of-task context-window size (what the next task carries), as
        # opposed to the result-level usage which SUMS input over every turn.
        last_window: Optional[int] = None
        for line in stdout.strip().split("\n"):
            line = line.strip()
            if not line:
                continue
            try:
                event = json.loads(line)
            except (json.JSONDecodeError, TypeError):
                continue
            if event.get("type") == "assistant":
                turn_usage = event.get("message", {}).get("usage", {})
                if turn_usage:
                    last_window = _context_tokens(turn_usage)
            if event.get("type") == "result":
                result_event = event

        if not result_event:
            logger.debug("No 'result' event in claude stream-json output")
            return {"result_text": stdout}

        usage = result_event.get("usage", {})
        return {
            "session_id": result_event.get("session_id"),
            "num_turns": result_event.get("num_turns"),
            "input_tokens": usage.get("input_tokens"),
            "output_tokens": usage.get("output_tokens"),
            # Billed input, summed across turns (matches total_cost_usd).
            "context_tokens": _context_tokens(usage),
            # True carried-context window (last turn); the compaction trigger.
            "context_window_tokens": last_window,
            "cost_usd": result_event.get("total_cost_usd"),
            "result_text": result_event.get("result", ""),
        }

    async def _run_session_turn(
        self, message: str, workspace: Path
    ) -> Dict[str, Any]:
        """Run a single --resume turn (json output) against the live session.

        Used for out-of-band maintenance turns like compaction. Returns the
        parsed metadata dict plus a "returncode" key.
        """
        cmd = [
            "claude",
            "-p",
            message,
            "--model",
            self.model,
            "--output-format",
            "json",
            "--dangerously-skip-permissions",
            "--resume",
            self._session_id,
        ]
        if self.no_skills:
            cmd += ["--disable-slash-commands", "--setting-sources",
                    "project,local"]

        env = {k: v for k, v in os.environ.items() if k != "CLAUDECODE"}
        process = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=str(workspace),
            env=env,
        )
        stdout_bytes, _ = await asyncio.wait_for(
            process.communicate(), timeout=self.timeout
        )
        metadata = self._parse_output(
            stdout_bytes.decode("utf-8", errors="replace")
        )
        metadata["returncode"] = process.returncode
        return metadata

    async def maybe_compact(
        self,
        last_window_tokens,
        position: int,
        workspace: Path,
    ):
        """Eagerly compact the persistent session between tasks.

        When the carried context (approximated by the last task's input tokens)
        exceeds ``self.compact_threshold``, drive an explicit /compact turn so
        the *next* task starts from a summarized context. This preempts the
        CLI's native auto-compaction (which would otherwise fire mid-task near
        the ~1M window). If /compact is not honored in headless mode, fall back
        to a we-driven summarize-and-reseed turn; if that also fails, log loudly
        rather than silently deferring to native auto-compaction.

        Returns a record ``{position, task_id?, before_tokens, after_tokens,
        method}`` or None if no compaction was performed.
        """
        if self.session_mode != "persistent" or not self.compact_threshold:
            return None
        if (not last_window_tokens
                or last_window_tokens <= self.compact_threshold):
            return None
        if not self._session_id:
            return None

        before = last_window_tokens
        logger.info(
            f"Eager compaction at position {position}: carried context "
            f"~{before} tokens > threshold {self.compact_threshold}"
        )

        # Primary: the CLI's built-in /compact slash command.
        method = "compact"
        try:
            meta = await self._run_session_turn("/compact", workspace)
            if meta.get("returncode") != 0:
                raise RuntimeError(f"/compact returned {meta.get('returncode')}")
        except Exception as e:
            logger.warning(
                f"/compact failed in headless mode ({e}); falling back to a "
                f"summarize-and-reseed turn"
            )
            method = "summarize-reseed"
            try:
                meta = await self._run_session_turn(
                    "We are pausing between tasks. Summarize concisely "
                    "everything worth remembering for the tasks ahead: q "
                    "idioms that worked, mistakes to avoid, and quirks of the "
                    "test harness. We will continue from this summary.",
                    workspace,
                )
                if meta.get("returncode") != 0:
                    raise RuntimeError(
                        f"summarize turn returned {meta.get('returncode')}"
                    )
            except Exception as e2:
                # Do NOT silently fall back to native auto-compaction.
                logger.error(
                    f"Eager compaction FAILED at position {position} "
                    f"(both /compact and summarize-reseed): {e2}. Context will "
                    f"keep growing and the CLI may auto-compact mid-task."
                )
                return {
                    "position": position,
                    "before_tokens": before,
                    "after_tokens": None,
                    "method": "failed",
                }

        # Track a possibly-forked session id from the maintenance turn.
        self._session_id = meta.get("session_id") or self._session_id
        return {
            "position": position,
            "before_tokens": before,
            # The compaction turn itself still reads the full pre-compaction
            # context, so its own usage isn't the post size. The real drop shows
            # up as the NEXT task's context_tokens in the learning curve.
            "after_tokens": None,
            "method": method,
        }
