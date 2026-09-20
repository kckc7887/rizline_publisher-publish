"""Offline execution-policy checks for the real publication workflow.

Read only the YAML blocks needed here; do not add a YAML dependency or execute
workflow shell code. The expression interpreter accepts this workflow's small
GitHub Actions subset and rejects unfamiliar syntax instead of using eval().
"""
import ast
import re
import shlex
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path


WORKFLOW = Path(__file__).resolve().parents[1] / ".github/workflows/publish.yml"


def block(text, key, indent):
    lines = text.splitlines()
    matches = [index for index, line in enumerate(lines) if line == " " * indent + key + ":"]
    if len(matches) != 1:
        raise AssertionError(f"Expected one {key!r} block at indentation {indent}")
    start = matches[0] + 1
    end = start
    while end < len(lines):
        line = lines[end]
        if line.strip() and not line.lstrip().startswith("#") and len(line) - len(line.lstrip()) <= indent:
            break
        end += 1
    return "\n".join(lines[start:end])


def scalar(text, key, indent, default=None):
    matches = re.findall(r"^" + " " * indent + re.escape(key) + r": (.+)$", text, re.MULTILINE)
    if not matches and default is not None:
        return default
    if len(matches) != 1:
        raise AssertionError(f"Expected one {key!r} value at indentation {indent}")
    value = matches[0].strip()
    if value.startswith(("'", '"')):
        return ast.literal_eval(value)
    return value


def steps(job):
    return [part for part in re.split(r"(?=^      - )", job, flags=re.MULTILINE) if part.startswith("      - ")]


def command(step):
    value = scalar(step, "run", 8, "")
    if value != "|":
        return value
    body = step.split("        run: |\n", 1)[1]
    return "\n".join(line[10:] for line in body.splitlines() if line.startswith("          "))


def evaluate(expression, context):
    expression = expression.removeprefix("${{").removesuffix("}}").strip()
    expression = expression.replace("&&", " and ").replace("||", " or ")
    tree = ast.parse(expression, mode="eval")

    def visit(node):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "always" and not node.args and not node.keywords:
            return True
        if isinstance(node, ast.Constant) and isinstance(node.value, (str, bool)):
            return node.value
        if isinstance(node, ast.Name):
            if node.id in ("true", "false"):
                return node.id == "true"
            if node.id in context:
                return context[node.id]
        if isinstance(node, ast.Attribute):
            values = visit(node.value)
            if isinstance(values, dict):
                # Missing workflow_dispatch inputs evaluate to empty on schedule.
                return values.get(node.attr, "")
        if isinstance(node, ast.BoolOp):
            result = visit(node.values[0])
            for operand in node.values[1:]:
                if isinstance(node.op, ast.Or) and result:
                    return result
                if isinstance(node.op, ast.And) and not result:
                    return result
                result = visit(operand)
            return result
        if isinstance(node, ast.Compare) and len(node.ops) == 1:
            left, right = visit(node.left), visit(node.comparators[0])
            if isinstance(left, str) and isinstance(right, str):
                left, right = left.casefold(), right.casefold()
            if isinstance(node.ops[0], ast.Eq):
                return left == right
            if isinstance(node.ops[0], ast.NotEq):
                return left != right
        raise AssertionError(f"Unsupported workflow expression: {ast.dump(node)}")

    return visit(tree.body)


def context(event, ref="refs/heads/main", **inputs):
    return {"github": {"event_name": event, "ref": ref}, "inputs": inputs}


class WorkflowTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.workflow = WORKFLOW.read_text(encoding="utf-8")
        cls.build = block(cls.workflow, "build", 2)
        cls.publish = block(cls.workflow, "publish", 2)
        cls.build_steps = steps(cls.build)
        cls.publish_steps = steps(cls.publish)

    def one_step(self, candidates, predicate):
        found = [step for step in candidates if predicate(command(step))]
        self.assertEqual(len(found), 1)
        return found[0]

    def test_daily_schedule_is_once_at_beijing_twenty(self):
        schedule = block(block(self.workflow, "on", 0), "schedule", 2)
        crons = re.findall(r"^    - cron: ['\"]([^'\"]+)['\"]$", schedule, re.MULTILINE)
        self.assertEqual(len(crons), 1)
        minute, hour, day, month, weekday = crons[0].split()
        self.assertEqual((day, month, weekday), ("*", "*", "*"))
        utc_run = datetime(2026, 9, 14, int(hour), int(minute), tzinfo=timezone.utc)
        beijing_run = utc_run.astimezone(timezone(timedelta(hours=8)))
        self.assertEqual((beijing_run.hour, beijing_run.minute), (20, 0))

    def test_event_matrix_controls_upload_branch_guard_and_success_summary(self):
        upload = self.one_step(self.publish_steps, lambda run: "rizline_publisher publish --execute" in run)
        guard = self.one_step(self.build_steps, lambda run: "exit 1" in run)
        summary = self.one_step(self.publish_steps, lambda run: "发布完成" in run)
        build_summary = self.one_step(self.build_steps, lambda run: "EXECUTE" in run)
        validation = self.one_step(self.publish_steps, lambda run: "rizline_publisher validate --release" in run)
        download = next(step for step in self.publish_steps if "uses: actions/download-artifact@" in step)
        # Invalid-branch schedule is defensive: GitHub normally schedules main only.
        cases = [
            (context("schedule"), True, False),
            (context("schedule", "refs/heads/feature"), False, True),
            (context("workflow_dispatch", execute=False), False, False),
            (context("workflow_dispatch", execute=True), True, False),
            (context("workflow_dispatch", "refs/heads/feature", execute=False), False, False),
            (context("workflow_dispatch", "refs/heads/feature", execute=True), False, True),
        ]
        for values, should_upload, should_reject in cases:
            with self.subTest(values=values):
                build_enabled = bool(evaluate(scalar(self.build, "if", 4, "true"), values))
                publish_enabled = bool(evaluate(scalar(self.publish, "if", 4, "true"), values))
                self.assertTrue(build_enabled)
                # Preview must still download and validate the release artifact.
                self.assertTrue(publish_enabled)
                self.assertEqual(bool(evaluate(scalar(guard, "if", 8, "true"), values)), should_reject)
                self.assertEqual(bool(evaluate(scalar(upload, "if", 8, "true"), values)), should_upload)
                self.assertEqual(bool(evaluate(scalar(summary, "if", 8, "true"), values)), should_upload)
                for step in (download, validation):
                    self.assertTrue(bool(evaluate(scalar(step, "if", 8, "true"), values)))
                if not should_reject:
                    intent = scalar(block(build_summary, "env", 8), "EXECUTE", 10)
                    self.assertEqual(bool(evaluate(intent, values)), should_upload)
        self.assertEqual(scalar(self.publish, "needs", 4), "build")
        self.assertLess(self.build_steps.index(guard), next(index for index, step in enumerate(self.build_steps) if "uses: actions/checkout@" in step))

    def test_schedule_workers_reach_both_commands_without_manual_inputs(self):
        publisher_steps = [step for step in self.build_steps + self.publish_steps if "rizline_publisher publish" in command(step)]
        self.assertEqual(len(publisher_steps), 2)
        cases = [(context("schedule"), "16"), (context("workflow_dispatch", upload_workers=""), "16")]
        cases.extend((context("workflow_dispatch", upload_workers=value), value) for value in ("1", "4", "8", "12", "16"))
        for step in publisher_steps:
            env = block(step, "env", 8)
            expression = scalar(env, "UPLOAD_WORKERS", 10)
            run = next(line for line in command(step).splitlines() if "rizline_publisher publish" in line)
            self.assertEqual(run.count("$UPLOAD_WORKERS"), 1)
            for values, expected in cases:
                with self.subTest(command=run, values=values):
                    argv = shlex.split(run.replace("$UPLOAD_WORKERS", str(evaluate(expression, values))))
                    self.assertEqual(argv[argv.index("--workers") + 1], expected)
        inputs = block(block(block(self.workflow, "on", 0), "workflow_dispatch", 2), "inputs", 4)
        self.assertEqual(scalar(block(inputs, "upload_workers", 6), "default", 8), "16")
        execute = block(inputs, "execute", 6)
        self.assertEqual(scalar(execute, "type", 8), "boolean")
        self.assertEqual(scalar(execute, "default", 8), "false")

    def test_only_two_user_secrets_are_needed_and_only_upload_receives_them(self):
        upload = self.one_step(self.publish_steps, lambda run: "rizline_publisher publish --execute" in run)
        expected = {"AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY"}
        secret_refs = re.findall(r"\bsecrets\.([A-Za-z_][A-Za-z_0-9]*)", self.workflow)
        self.assertCountEqual(secret_refs, expected)
        self.assertFalse(re.search(r"\bvars(?:\.|\[)", self.workflow))
        env = block(upload, "env", 8)
        for name in expected:
            self.assertEqual(evaluate(scalar(env, name, 10), {"secrets": {name: "test-marker"}}), "test-marker")
        self.assertNotRegex(self.workflow.replace(upload, ""), r"\bsecrets(?:\.|\[)")

    def test_publication_runs_share_one_non_cancelling_lock(self):
        concurrency = block(self.workflow, "concurrency", 0)
        group = scalar(concurrency, "group", 2)
        self.assertTrue(group.strip())
        # No branch/event/run suffix: manual and scheduled runs share the same lock.
        self.assertNotIn("${{", group)
        self.assertEqual(scalar(concurrency, "cancel-in-progress", 2), "false")

    def test_build_installs_pinned_vgmstream_before_release_build(self):
        install = self.one_step(self.build_steps, lambda run: "vgmstream-linux.zip" in run)
        release = self.one_step(self.build_steps, lambda run: "rizline_publisher build" in run)
        self.assertIn("r2117", command(install))
        self.assertIn("2f98c77f756079f63fbd119939067f1ed461d77e70993bc4cc372736d859c84a", command(install))
        self.assertIn("ffmpeg", command(install))
        self.assertLess(self.build_steps.index(install), self.build_steps.index(release))

    def test_parse_concurrency_reaches_import_build_and_local_validation(self):
        candidates = [step for step in self.build_steps if "$PARSE_WORKERS" in command(step)]
        self.assertEqual(len(candidates), 2)
        for step in candidates:
            expression = scalar(block(step, "env", 8), "PARSE_WORKERS", 10)
            for event, value, expected in (("schedule", "", "4"), ("workflow_dispatch", "", "4"), ("workflow_dispatch", "8", "8")):
                self.assertEqual(evaluate(expression, context(event, parse_workers=value)), expected)
            for line in command(step).splitlines():
                if "$PARSE_WORKERS" in line:
                    argv = shlex.split(line.replace("$PARSE_WORKERS", "8"))
                    self.assertEqual(argv[argv.index("--workers") + 1], "8")

    def test_real_publication_and_failure_receipts_are_archived_even_after_failure(self):
        artifacts = [step for step in self.publish_steps if "uses: actions/upload-artifact@" in step]
        self.assertEqual(len(artifacts), 2)
        for step in artifacts:
            expression = scalar(step, "if", 8)
            self.assertIn("always()", expression)
            self.assertTrue(evaluate(expression, context("schedule")))
            self.assertFalse(evaluate(expression, context("workflow_dispatch", execute=False)))
        self.assertTrue(any("path: work/publication-release/" in step for step in artifacts))
        self.assertTrue(any("work/publication-report.json" in step and "work/cleanup-receipts/" in step for step in artifacts))
        upload = self.one_step(self.publish_steps, lambda run: "rizline_publisher publish --execute" in run)
        self.assertIn("set -o pipefail", command(upload))
        self.assertIn("--delta-only", command(upload))
        self.assertEqual(scalar(self.publish, "timeout-minutes", 4), "90")
        summary = self.one_step(self.publish_steps, lambda run: "发布结果" in run)
        self.assertIn("always()", scalar(summary, "if", 8))
        self.assertIn("currentSwitched", command(summary))
        self.assertIn("'failed'", command(summary))


if __name__ == "__main__":
    unittest.main()
